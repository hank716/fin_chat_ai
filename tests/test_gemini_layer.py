"""spec 005 測試基線補齊：結構化 JSON 解析、400 不重試、usage 欄位、分類器 fail-open、
明確快取降級、兩段式 grounded、缺 API key。

503 重試 / 429 fail-fast 另見 tests/test_gemini_retry.py。
"""
from __future__ import annotations

import pytest

from ai import gemini_cache
from ai import gemini_client as gc


class _Resp:
    def __init__(self, status=200, body=None, text="err"):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body


def _body(text, **usage):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": usage or {"promptTokenCount": 1, "candidatesTokenCount": 1}}


@pytest.fixture(autouse=True)
def _keyed(monkeypatch):
    monkeypatch.setattr(gc.settings, "gemini_api_key", "k")
    monkeypatch.setattr(gc.monitor, "mark", lambda *a, **k: None)
    monkeypatch.setattr(gc, "_note_model_version", lambda *a, **k: None)
    monkeypatch.setattr(gc._generate_json.retry, "sleep", lambda *_: None)
    monkeypatch.setattr(gc.generate_text_with_candidate.retry, "sleep", lambda *_: None)


def _post_returning(monkeypatch, resp):
    calls = []

    def post(url, **kw):
        calls.append(kw.get("json"))
        return resp

    monkeypatch.setattr(gc.httpx, "post", post)
    return calls


# ── T014 合法 JSON → dict+usage；非 JSON / 空 / 無 candidates → GeminiError ──

def test_valid_json_returns_dict_and_usage(monkeypatch):
    _post_returning(monkeypatch, _Resp(body=_body('{"a": 1}', promptTokenCount=7, candidatesTokenCount=3)))
    raw, usage = gc._generate_json("p", {})
    assert raw == {"a": 1} and usage["input_tokens"] == 7 and usage["output_tokens"] == 3


@pytest.mark.parametrize("body", [_body("not json"), _body("   "), {"candidates": []}, {}])
def test_bad_payloads_raise_gemini_error(monkeypatch, body):
    _post_returning(monkeypatch, _Resp(body=body))
    with pytest.raises(gc.GeminiError):
        gc._generate_json("p", {})


# ── T015 400 → BadRequest 且不重試（503/429 見 test_gemini_retry）──

@pytest.mark.parametrize("fn", [lambda: gc._generate_json("p", {}),
                                lambda: gc.generate_text_with_candidate("p")])
def test_400_is_bad_request_and_not_retried(monkeypatch, fn):
    calls = _post_returning(monkeypatch, _Resp(status=400, text="cachedContent incompatible"))
    with pytest.raises(gc.GeminiBadRequest):
        fn()
    assert len(calls) == 1


def test_other_http_errors_are_generic_and_not_retried(monkeypatch):
    calls = _post_returning(monkeypatch, _Resp(status=500))
    with pytest.raises(gc.GeminiError) as exc:
        gc._generate_json("p", {})
    assert not isinstance(exc.value, (gc.GeminiBadRequest, gc.GeminiUnavailable)) and len(calls) == 1


# ── T016 usage：thinking 計入 output、cached 與 tool 分開 ──

def test_usage_counts_thinking_and_separates_cache_and_tools():
    usage = gc._usage_of({"usageMetadata": {
        "promptTokenCount": 1000, "cachedContentTokenCount": 600, "candidatesTokenCount": 50,
        "thoughtsTokenCount": 200, "toolUsePromptTokenCount": 30}})
    assert usage == {"input_tokens": 1000, "cached_tokens": 600, "output_tokens": 250, "tool_tokens": 30}


def test_usage_missing_metadata_is_all_zero():
    assert set(gc._usage_of({}).values()) == {0}


# ── T017 分類器：HTTP 失敗或解析失敗都 fail-open ──

@pytest.mark.parametrize("resp", [_Resp(status=500), _Resp(status=429), _Resp(body=_body("oops"))])
def test_classifier_fails_open(monkeypatch, resp):
    _post_returning(monkeypatch, resp)
    assert gc.classify_finance_intent("台積電") == (True, {})


def test_classifier_reports_non_financial(monkeypatch):
    _post_returning(monkeypatch, _Resp(body=_body('{"is_financial": false}')))
    ok, usage = gc.classify_finance_intent("晚餐吃什麼")
    assert ok is False and usage


# ── T018 明確快取：建立失敗回 None；成功寫 redis 且 TTL 早於 cachedContents ──

class _Redis:
    def __init__(self, broken=False):
        self.broken, self.store, self.ex = broken, {}, {}

    def get(self, k):
        if self.broken:
            raise ConnectionError("redis down")
        return self.store.get(k)

    def set(self, k, v, ex=None):
        if self.broken:
            raise ConnectionError("redis down")
        self.store[k], self.ex[k] = v, ex


@pytest.fixture
def cache_env(monkeypatch):
    monkeypatch.setattr(gemini_cache.settings, "enable_gemini_explicit_cache", True)
    monkeypatch.setattr(gemini_cache.settings, "gemini_cache_ttl_seconds", 3600)
    r = _Redis()
    monkeypatch.setattr(gemini_cache, "redis_client", r)
    return r


def test_cache_create_400_degrades_to_none(cache_env, monkeypatch):
    monkeypatch.setattr(gemini_cache.httpx, "post", lambda *a, **k: _Resp(status=400, text="too short"))
    assert gemini_cache.get_or_create_qa_cache("rid", "static", "m") is None


def test_cache_connection_error_degrades_to_none(cache_env, monkeypatch):
    def boom(*a, **k):
        raise gemini_cache.httpx.ConnectError("down")

    monkeypatch.setattr(gemini_cache.httpx, "post", boom)
    assert gemini_cache.get_or_create_qa_cache("rid", "static", "m") is None


def test_cache_success_is_stored_with_shorter_ttl_and_reused(cache_env, monkeypatch):
    posts = []
    monkeypatch.setattr(gemini_cache.httpx, "post",
                        lambda url, json=None, **k: posts.append(json) or _Resp(body={"name": "cachedContents/abc"}))
    assert gemini_cache.get_or_create_qa_cache("rid", "static", "m") == "cachedContents/abc"
    key = gemini_cache._redis_key("m", "rid")
    assert cache_env.ex[key] == 3600 - 120                   # 比 cachedContents 早 2 分鐘過期
    assert posts[0]["ttl"] == "3600s" and posts[0]["tools"] == gc.SEARCH_TOOLS
    assert gemini_cache.get_or_create_qa_cache("rid", "static", "m") == "cachedContents/abc"
    assert len(posts) == 1                                   # 第二次直接用 redis 裡的名稱


def test_cache_survives_redis_outage(monkeypatch):
    """redis 掛掉時不拋：照樣向 Gemini 建快取（只是存不了名稱）。"""
    monkeypatch.setattr(gemini_cache.settings, "enable_gemini_explicit_cache", True)
    monkeypatch.setattr(gemini_cache, "redis_client", _Redis(broken=True))
    monkeypatch.setattr(gemini_cache.httpx, "post", lambda *a, **k: _Resp(body={"name": "cachedContents/x"}))
    assert gemini_cache.get_or_create_qa_cache("rid", "static", "m") == "cachedContents/x"


def test_cache_disabled_or_without_key_is_none(cache_env, monkeypatch):
    monkeypatch.setattr(gemini_cache.settings, "enable_gemini_explicit_cache", False)
    assert gemini_cache.get_or_create_qa_cache("rid", "s", "m") is None
    monkeypatch.setattr(gemini_cache.settings, "enable_gemini_explicit_cache", True)
    monkeypatch.setattr(gemini_cache.settings, "gemini_api_key", " ")
    assert gemini_cache.get_or_create_qa_cache("rid", "s", "m") is None


# ── T019 兩段式 grounded：研究段開搜尋、格式化段不開，回兩段 usage ──

def test_grounded_brief_runs_research_then_structuring(monkeypatch):
    from datetime import date
    order = []

    def gen_text(prompt, model=None, use_search=False, **k):
        order.append(("research", model, use_search))
        return "分析稿", {"input_tokens": 10}

    def gen_json(prompt, schema, model=None, temperature=0.4, **k):
        order.append(("structure", model, temperature))
        assert "分析稿" in prompt
        return {"headline": "h", "data_as_of": str(date(2026, 10, 14))}, {"input_tokens": 3}

    monkeypatch.setattr(gc, "generate_text", gen_text)
    monkeypatch.setattr(gc, "_generate_json", gen_json)
    result, research, struct = gc.analyze_full_brief_grounded({"as_of": "2026-10-14"})
    s = gc.settings
    assert order == [("research", s.gemini_model_brief, True), ("structure", s.gemini_model_qa, 0.1)]
    assert (research, struct) == ({"input_tokens": 10}, {"input_tokens": 3})
    assert result.headline == "h"


# ── T020 缺 GEMINI_API_KEY → GeminiError，不發請求 ──

@pytest.mark.parametrize("fn", [lambda: gc._generate_json("p", {}),
                                lambda: gc.generate_text_with_candidate("p")])
def test_missing_api_key_raises_without_http(monkeypatch, fn):
    monkeypatch.setattr(gc.settings, "gemini_api_key", "  ")
    calls = _post_returning(monkeypatch, _Resp())
    with pytest.raises(gc.GeminiError, match="GEMINI_API_KEY"):
        fn()
    assert calls == []
