"""spec 008 測試基線：/ask 的預算放行、意圖早退、無晨報、快取降級、FinMind 退避、討論串記憶。

全部離線：redis / LLM / 晨報 / FinMind 都換成記憶體替身，直接呼叫 handler。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from ai import gemini_cache, gemini_client
from ai.gemini_client import GeminiBadRequest
from api import ask as ask_api
from chat import history
from cost import tracker as ct
from data_sources.finmind_loader import FinMindBackoff
from processor import fundamentals

DISCLAIMER = "非保證、請自行判斷風險"


class _FakeRedis:
    """同時扮演成本桶（incrbyfloat/get）與討論串記憶（list）。"""

    def __init__(self):
        self.store: dict = {}
        self.ttls: dict[str, int] = {}

    def incrbyfloat(self, key, amount):
        self.store[key] = self.store.get(key, 0.0) + float(amount)
        return self.store[key]

    def get(self, key):
        return None if key not in self.store else str(self.store[key])

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def rpush(self, key, value):
        self.store.setdefault(key, []).append(value)

    def ltrim(self, key, start, end):
        lst = self.store.get(key, [])
        self.store[key] = lst[start:] if end == -1 else lst[start:end + 1]

    def lrange(self, key, start, end):
        lst = self.store.get(key, [])
        return lst[start:] if end == -1 else lst[start:end + 1]


class _BrokenRedis:
    def __getattr__(self, name):
        def boom(*_a, **_k):
            raise ConnectionError("redis down")
        return boom


class _FakeLLM:
    name = "anthropic"

    def __init__(self, answer="台積電技術面偏多。"):
        self.answer = answer
        self.calls = 0

    def answer_question(self, system, user, cacheable=False):  # noqa: ARG002
        self.calls += 1
        return self.answer, {"input_tokens": 10, "output_tokens": 5}


REPORT = {"features": {"tw": {"stocks": {"2330": {"close": 900}}}}}


@pytest.fixture
def env(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(ct, "redis_client", r)
    monkeypatch.setattr(history, "redis_client", r)
    s = ct.settings
    monkeypatch.setattr(s, "monthly_cost_limit_twd", 800)
    monkeypatch.setattr(s, "daily_cost_limit_twd", 45)
    monkeypatch.setattr(s, "admin_token", "secret")
    monkeypatch.setattr(s, "enable_intent_filter", True)
    monkeypatch.setattr(ask_api.morning_brief, "latest_report_id", lambda: "morning_x")
    monkeypatch.setattr(ask_api.morning_brief, "load_report", lambda rid: REPORT)
    monkeypatch.setattr(ask_api.tw_features, "build_adhoc_symbol", lambda sym: None)
    monkeypatch.setattr(fundamentals, "build_fundamentals", lambda sym: {"rev_yoy": 0.1})
    monkeypatch.setattr(gemini_client, "classify_finance_intent", lambda q: (True, {"in": 1}))
    # 計價本身已由 test_cost_tracker 驗證；這裡只看「有沒有記、記了多少筆」
    monkeypatch.setattr(ct, "cost_of_usage", lambda usage, model, grounded=False:
                        0.01 if model == s.gemini_model_classifier else 1.0)
    llm = _FakeLLM()
    monkeypatch.setattr(ask_api, "get_decision_llm", lambda: llm)
    return {"redis": r, "llm": llm}


def _ask(question="2330 怎麼看", token=None, conversation_id=None):
    req = ask_api.AskRequest(user_id="u1", question=question, conversation_id=conversation_id)
    return asyncio.run(ask_api.ask(req, x_admin_token=token))


# ── T014 逾限：無權杖婉拒；有權杖放行且花費照常計入 ──

def test_over_budget_without_token_is_refused(env):
    env["redis"].store[ct._day_key()] = 45.0
    resp = _ask()
    assert resp.budget_exceeded and env["llm"].calls == 0


def test_over_budget_with_admin_token_runs_and_is_billed(env):
    env["redis"].store[ct._day_key()] = 45.0
    resp = _ask(token="secret")
    assert not resp.budget_exceeded and env["llm"].calls == 1
    assert resp.cost_twd == pytest.approx(1.01)                 # 問答 1.0 + 分類 0.01
    assert env["redis"].store[ct._day_key()] == pytest.approx(46.01)


# ── T015 非財務題早退只計分類成本；分類器失敗 fail-open ──

def test_non_financial_question_exits_after_classifier(env, monkeypatch):
    monkeypatch.setattr(gemini_client, "classify_finance_intent", lambda q: (False, {"in": 1}))
    resp = _ask("今天晚餐吃什麼")
    assert env["llm"].calls == 0
    assert resp.cost_twd == pytest.approx(0.01)
    assert env["redis"].store[ct._day_key()] == pytest.approx(0.01)


def test_classifier_failure_fails_open(monkeypatch):
    def broken(_payload, *_a, **_k):
        raise gemini_client.GeminiError("boom")

    # 走真的 classify_finance_intent（不套 env），讓它內部的 _generate_json 失敗
    monkeypatch.setattr(gemini_client, "_generate_json", broken)
    assert gemini_client.classify_finance_intent("台積電") == (True, {})


# ── T016 無晨報 → 404 ──

def test_no_report_is_404(env, monkeypatch):
    monkeypatch.setattr(ask_api.morning_brief, "latest_report_id", lambda: None)
    with pytest.raises(HTTPException) as exc:
        _ask()
    assert exc.value.status_code == 404


# ── T017 Gemini 明確快取 400 → 降級完整 prompt ──

def test_explicit_cache_bad_request_falls_back_to_full_prompt(monkeypatch):
    monkeypatch.setattr(ask_api, "build_qa_static_block", lambda report: "STATIC|")
    monkeypatch.setattr(gemini_cache, "get_or_create_qa_cache", lambda *a, **k: "cachedContents/1")
    calls = []

    def generate_text(prompt, use_search=False, cached_content=None):
        calls.append((prompt, cached_content))
        if cached_content:
            raise GeminiBadRequest("cache incompatible with tools")
        return "答", {"promptTokenCount": 1}

    monkeypatch.setattr(gemini_client, "generate_text", generate_text)
    assert ask_api._ask_gemini("rid", REPORT, "Q") == ("答", {"promptTokenCount": 1})
    assert calls == [("Q", "cachedContents/1"), ("STATIC|Q", None)]


# ── T018 FinMind 退避 → 缺基本面但照常回答 ──

def test_finmind_backoff_still_answers(env, monkeypatch):
    def backoff(sym):
        raise FinMindBackoff("402 hourly quota")

    monkeypatch.setattr(fundamentals, "build_fundamentals", backoff)
    resp = _ask()
    assert env["llm"].calls == 1 and resp.answer.startswith("台積電技術面偏多")


# ── T019 討論串記憶：截斷、限輪數、TTL；redis 故障回空不拋 ──

def test_history_truncates_caps_turns_and_sets_ttl(env):
    r = env["redis"]
    for i in range(history.MAX_TURNS + 3):
        history.append("t1", f"q{i}" + "長" * 600, "a" * 2000)
    turns = history.load("t1")
    assert len(turns) == history.MAX_TURNS
    assert turns[-1]["q"].startswith(f"q{history.MAX_TURNS + 2}")   # 留最新的
    assert len(turns[0]["q"]) == 500 and len(turns[0]["a"]) == 1200
    assert r.ttls[history._key("t1")] == 3 * 24 * 3600


def test_history_without_conversation_is_stateless(env):
    history.append("", "q", "a")
    assert history.load("") == [] and env["redis"].store == {}


def test_history_redis_failure_is_silent(monkeypatch):
    monkeypatch.setattr(history, "redis_client", _BrokenRedis())
    history.append("t1", "q", "a")                               # 不拋
    assert history.load("t1") == []


def test_history_skips_corrupt_entries(env):
    env["redis"].store[history._key("t1")] = ["not json", json.dumps({"q": "q", "a": "a"})]
    assert history.load("t1") == [{"q": "q", "a": "a"}]


# ── T020 記憶存乾淨答案（不含免責句）；回覆含免責句 ──

def test_memory_stores_clean_answer_reply_has_disclaimer(env):
    resp = _ask(conversation_id="t9")
    assert DISCLAIMER in resp.answer
    stored = history.load("t9")
    assert stored == [{"q": "2330 怎麼看", "a": "台積電技術面偏多。"}]
    assert DISCLAIMER not in stored[0]["a"]


def test_general_channel_writes_no_memory(env):
    _ask()
    assert not any(str(k).startswith("chat:hist:") for k in env["redis"].store)


# ── spec 026：/ask 走 OpenAI ───────────────────────────────────────────────

class _FakeOpenAILLM(_FakeLLM):
    name = "openai"


def test_openai_provider_answers_and_bills_openai(env, monkeypatch):
    llm = _FakeOpenAILLM("OpenAI 回答")
    monkeypatch.setattr(ask_api, "get_decision_llm", lambda: llm)
    priced = []

    def spy(usage, model, grounded=False):
        priced.append((model, grounded))
        return 0.01 if model == ct.settings.gemini_model_classifier else 1.0

    monkeypatch.setattr(ct, "cost_of_usage", spy)
    resp = _ask()
    assert llm.calls == 1
    assert resp.answer.startswith("OpenAI 回答")
    assert priced[-1] == (ct.settings.openai_model_chat, False)
    assert ct.month_by_provider()["openai"] == pytest.approx(1.0)
    assert ct.month_by_provider()["gemini"] == pytest.approx(0.01)   # 意圖分類仍是 Gemini


def test_openai_provider_respects_daily_limit(env, monkeypatch):
    llm = _FakeOpenAILLM()
    monkeypatch.setattr(ask_api, "get_decision_llm", lambda: llm)
    env["redis"].store[ct._day_key()] = 45.0
    resp = _ask()
    assert resp.budget_exceeded and llm.calls == 0


def test_openai_answer_question_only_offers_web_search(monkeypatch):
    from ai import llm_client, openai_client

    seen = {}

    def fake_generate_text(**kw):
        seen.update(kw)
        return "答", {}

    monkeypatch.setattr(openai_client, "generate_text", fake_generate_text)
    llm_client.OpenAIDecisionLLM().answer_question("sys", "user", cacheable=True)
    assert seen["model"] == llm_client.settings.openai_model_chat
    assert seen["effort"] == llm_client.settings.openai_effort
    assert seen["search_limit"] == llm_client.settings.openai_chat_search_uses
    assert "fetch_session" not in seen
