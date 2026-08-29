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
