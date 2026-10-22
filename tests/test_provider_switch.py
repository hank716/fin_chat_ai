"""決策層供應商分派與降級（spec 022 WP1/WP3）。

晨報是無人值守的每日排程：換供應商不得讓它變成有機率整份產不出來。
這裡釘住 (a) 分派正確、(b) Claude 失敗會退回 Gemini、(c) 降級時 fact_checks 誠實留空。
"""
from __future__ import annotations

import pytest

from ai import llm_client
from ai.errors import LLMQuotaExceeded
from ai.retrieval import FactsPack
from ai.schemas import BriefDraft


def _draft(headline="今日結論") -> BriefDraft:
    return BriefDraft(
        headline=headline, sections=[], tw_watchlist=[], tw_caution=[],
        risks=[], follow_ups=[], news_digest=[], fact_checks=[],
        data_as_of="2026-08-06", sources=[],
    )


def test_dispatch_anthropic_by_default(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "llm_decision_provider", "anthropic")
    assert llm_client.get_decision_llm().name == "anthropic"


def test_dispatch_gemini_when_configured(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "llm_decision_provider", "gemini")
    assert llm_client.get_decision_llm().name == "gemini"


def test_unknown_provider_fails_fast(monkeypatch):
    """打錯字不能靜默走到別的供應商——那會讓成本與品質同時失控且難以察覺。"""
    monkeypatch.setattr(llm_client.settings, "llm_decision_provider", "gpt")
    with pytest.raises(ValueError, match="LLM_DECISION_PROVIDER"):
        llm_client.get_decision_llm()


def test_brief_falls_back_to_gemini_when_claude_fails(monkeypatch):
    """Claude 配額用盡 → 退回 Gemini 兩段式，晨報照樣產出。"""
    from reports import morning_brief

    class _Failing:
        name = "anthropic"

        def draft_brief(self, *a, **k):
            raise LLMQuotaExceeded("配額用盡")

    class _Fallback:
        name = "gemini"

        def draft_brief(self, *a, **k):
            # 降級路徑不做查證：attempts 恆為空，與 GeminiDecisionLLM 實作一致
            return _draft("降級產出"), {"input_tokens": 10, "output_tokens": 5}, []

    monkeypatch.setattr(
        morning_brief, "get_decision_llm",
        lambda provider=None: _Fallback() if provider == "gemini" else _Failing(),
    )

    decision = morning_brief._decide_brief({}, FactsPack(), "")
    assert decision.result.headline == "降級產出"
    assert decision.provider == "gemini-fallback"
    # 降級路徑不查證 → fact_checks 與 fetch_attempts 都空，是**誠實訊號**，不是遺漏
    assert decision.fact_checks == []
    assert decision.fetch_attempts == []
    assert decision.usage["input_tokens"] == 10


def test_fallback_not_attempted_when_already_on_gemini(monkeypatch):
    """已經是降級路徑本身就沒有更下層可退，直接讓錯誤浮上來。"""
    from reports import morning_brief

    class _Failing:
        name = "gemini"

        def draft_brief(self, *a, **k):
            raise LLMQuotaExceeded("配額用盡")

    monkeypatch.setattr(morning_brief, "get_decision_llm", lambda provider=None: _Failing())
    with pytest.raises(LLMQuotaExceeded):
        morning_brief._decide_brief({}, FactsPack(), "")


def test_draft_to_result_drops_ml_scored_fields():
    """draft 不含 risk/conviction/size——那三個由本地 ML 事後填，LLM 產了只會被覆蓋。"""
    from reports import morning_brief

    result = morning_brief._draft_to_result(_draft())
    assert result.headline == "今日結論"
    # BriefResult 這三欄的預設就是 None，等著打分階段填
    dumped = result.model_dump()
    assert "fact_checks" not in dumped


def test_result_to_draft_strips_ml_fields():
    """Gemini 降級回來的 BriefResult 轉 draft 時要把 ML 欄位剝掉（draft schema 沒有它們）。"""
    from ai.schemas import BriefResult, WatchItem

    result = BriefResult(
        headline="h", data_as_of="2026-08-06",
        tw_watchlist=[WatchItem(symbol="2330", name="台積電", thesis="t",
                                risk_score=0.4, conviction_score=0.6, size_weight=0.2)],
    )
    draft = llm_client._result_to_draft(result)
    assert draft.tw_watchlist[0].symbol == "2330"
    assert draft.fact_checks == []


def test_fallback_carries_fetch_attempts_from_the_failed_primary(monkeypatch):
    """Claude 在倒下之前可能已經開過幾次原文——那些查證行為是真的發生過。

    `claude_client._with_attempts` 把它們掛在例外上，就是為了在降級處接住；
    不接的話，失敗那次的查證用量連同 usage 一起從遙測裡整段消失。
    """
    from ai.claude_client import FetchAttempt
    from reports import morning_brief

    done = [FetchAttempt(url="https://a.example/1", ok=True, error_code=None,
                         retrieved_at="2026-08-29T00:00:00Z"),
            FetchAttempt(url="https://b.example/2", ok=False,
                         error_code="url_not_accessible", retrieved_at=None)]

    class _Failing:
        name = "anthropic"

        def draft_brief(self, *a, **k):
            exc = LLMQuotaExceeded("配額用盡")
            exc.fetch_attempts = done
            raise exc

    class _Fallback:
        name = "gemini"

        def draft_brief(self, *a, **k):
            return _draft("降級產出"), {"input_tokens": 10}, []

    monkeypatch.setattr(
        morning_brief, "get_decision_llm",
        lambda provider=None: _Fallback() if provider == "gemini" else _Failing(),
    )
    decision = morning_brief._decide_brief({}, FactsPack(), "")
    assert [a.url for a in decision.fetch_attempts] == ["https://a.example/1",
                                                        "https://b.example/2"]


def test_fallback_report_has_no_verification_layer(monkeypatch):
    """降級路徑沒有 web_fetch＝**沒有查證這回事**，不是「線索全部查證失敗」。

    分不清楚的代價很具體：那些線索若被當成 unchecked_* 餵進 guardrail，會讓一篇與查證
    完全無關的降級晨報被整批刪掉 news_digest / sources，還配上一串重複的降級提示。
    """
    from reports import morning_brief

    facts = FactsPack(events=[
        {"claim": "某公司宣布擴產", "date": "2026-08-29",
         "source": "鉅亨網", "url": "https://n.example/1"},
    ])
    stats, outcomes = morning_brief._verification_stats(
        facts, [], {"input_tokens": 10}, [], active=False,
    )
    assert outcomes == []                      # 不得餵任何「未查證線索」給 guardrail
    assert stats["verification_active"] is False
    assert stats["outcomes"] == {} and stats["clues"] == []
    assert stats["fetch_limit"] == 0           # 上限 0＝沒掛工具，不是「額度為 0 的失敗」
    assert stats["facts_n"] == 1               # 召回層的既有遙測照常保留



# ── spec 026：openai 供應商 ────────────────────────────────────────────────

def test_dispatch_openai_when_configured(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "llm_decision_provider", "openai")
    llm = llm_client.get_decision_llm()
    assert llm.name == "openai"
    assert isinstance(llm, llm_client.OpenAIDecisionLLM)


def test_unknown_provider_message_lists_all_options(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "llm_decision_provider", "mistral")
    with pytest.raises(ValueError, match="anthropic / openai / gemini"):
        llm_client.get_decision_llm()


def _capture(monkeypatch, target, name, ret):
    seen = {}

    def fake(*a, **k):
        seen.update(k)
        return ret

    monkeypatch.setattr(target, name, fake)
    return seen


_FACTS = '[{"claim": "c", "date": "2026-10-21", "source": "s", "url": "https://a.example/x"}]'


def test_openai_and_anthropic_get_identical_prompts(monkeypatch):
    """A/B 公平性：兩邊吃逐字相同的 system / user prompt，差別只剩模型。"""
    from ai import claude_client, openai_client

    oa = _capture(monkeypatch, openai_client, "generate_structured", (_draft(), {}, [], "v"))
    cl = _capture(monkeypatch, claude_client, "generate_structured", (_draft(), {}, []))
    feats = {"as_of": "2026-10-22"}
    llm_client.OpenAIDecisionLLM().draft_brief(feats, _FACTS, "校準")
    llm_client.AnthropicDecisionLLM().draft_brief(feats, _FACTS, "校準")
    assert oa["system"] == cl["system"]
    assert oa["user_prompt"] == cl["user_prompt"]


def test_openai_whitelist_is_facts_pack_urls(monkeypatch):
    from ai import openai_client

    seen = _capture(monkeypatch, openai_client, "generate_structured", (_draft(), {}, [], None))
    llm_client.OpenAIDecisionLLM().draft_brief({}, _FACTS, None)
    session = seen["fetch_session"]
    assert session is not None
    _out, attempt = session.fetch("https://evil.example/y")
    assert attempt.reject_reason == "not_in_facts_pack"
    assert session._allowed == {"https://a.example/x"}
    assert [t.get("name") or t["type"] for t in seen["tools"]] == ["web_fetch", "web_search"]
    assert seen["effort"] == llm_client.settings.openai_brief_effort
    assert seen["search_limit"] == llm_client.settings.openai_brief_search_uses


def test_openai_frugal_mode_matches_anthropic_semantics(monkeypatch):
    from ai import openai_client

    seen = _capture(monkeypatch, openai_client, "generate_structured", (_draft(), {}, [], None))
    llm_client.OpenAIDecisionLLM().draft_brief({}, _FACTS, None, frugal=True)
    assert seen["tools"] == []
    assert seen["fetch_session"] is None
    assert seen["effort"] == "low"
    assert seen["search_limit"] == 0
    assert "本次無外部線索" in seen["user_prompt"]          # facts 傳的是 "[]"


def test_openai_records_model_version(monkeypatch):
    from ai import openai_client

    _capture(monkeypatch, openai_client, "generate_structured",
             (_draft(), {}, [], "gpt-6.1-sol-2026-10-01"))
    llm = llm_client.OpenAIDecisionLLM()
    llm.draft_brief({}, "[]", None)
    assert llm.last_model_version == "gpt-6.1-sol-2026-10-01"


def test_facts_urls_tolerates_garbage():
    assert llm_client._facts_urls("not json") == []
    assert llm_client._facts_urls('{"a": 1}') == []
    assert llm_client._facts_urls('[{"url": "u1"}, {"nourl": 1}, "x"]') == ["u1"]
