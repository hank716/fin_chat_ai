"""廣度召回層 facts pack（spec 022 WP2）。

重點只有一個：**查證不了的線索不該進決策層**。無來源的 event 一律丟棄，
有 groundingMetadata 就把 URL 補回去——漏掉 URL 等於決策層沒辦法 web_fetch 查證那條事實。
"""
from __future__ import annotations

import pytest

from ai import retrieval


def _chunks(*uris):
    return {"groundingMetadata": {"groundingChunks": [{"web": {"uri": u}} for u in uris]}}


def test_all_grounding_urls_is_not_truncated():
    """gemini_client._grounding_sources 的呼叫端只取前 4 筆（給 Discord 附連結用）；
    查證需要完整清單，第 5 筆之後不能掉。"""
    cand = _chunks(*[f"https://e{i}.test" for i in range(9)])
    assert len(retrieval._all_grounding_urls(cand)) == 9


def test_all_grounding_urls_dedupes():
    cand = _chunks("https://a.test", "https://a.test", "https://b.test")
    assert retrieval._all_grounding_urls(cand) == ["https://a.test", "https://b.test"]


def test_event_without_any_source_is_dropped():
    """模型沒填 url、grounding 也補不到 → 丟棄。查證不了的東西不進決策層。"""
    events = [{"claim": "無來源傳聞", "date": "2026-08-05", "source": "不明"}]
    assert retrieval._attach_urls(events, []) == []


def test_url_backfilled_from_grounding_in_order():
    """Gemini 常在 groundingMetadata 有來源、卻沒把 url 寫進 JSON 欄位。"""
    events = [
        {"claim": "A", "date": "2026-08-05", "source": "鉅亨"},
        {"claim": "B", "date": "2026-08-05", "source": "經濟日報"},
    ]
    out = retrieval._attach_urls(events, ["https://a.test", "https://b.test"])
    assert [e.url for e in out] == ["https://a.test", "https://b.test"]


def test_model_supplied_url_wins_over_grounding():
    events = [{"claim": "A", "date": "2026-08-05", "source": "鉅亨",
               "url": "https://explicit.test"}]
    out = retrieval._attach_urls(events, ["https://fallback.test"])
    assert out[0].url == "https://explicit.test"


def test_incomplete_event_is_dropped():
    """缺 date/source 的線索無法查證也無法標註，丟掉而不是硬塞空字串。"""
    events = [{"claim": "只有結論沒有時間"}]
    assert retrieval._attach_urls(events, ["https://a.test"]) == []


def test_verified_starts_empty():
    """verified 是留給決策層回填的欄位，召回層一律不碰。"""
    out = retrieval._attach_urls(
        [{"claim": "A", "date": "2026-08-05", "source": "s", "url": "https://a.test"}], [],
    )
    assert out[0].verified is None
    # 送進 prompt 時不帶 verified（那是輸出欄位，不是輸入）
    assert "verified" not in retrieval.FactsPack(events=out).to_prompt_json()


def test_fetch_facts_disabled_returns_empty(monkeypatch):
    """召回層失敗不得拖垮晨報——決策層只憑 features 仍要能產出。"""
    monkeypatch.setattr(retrieval.gemini_client, "generate_text_with_candidate",
                        lambda *a, **k: (_ for _ in ()).throw(retrieval.LLMError("boom")))
    pack, usage = retrieval.fetch_facts("2026-08-06")
    assert pack.events == []
    assert usage == {}


def test_fetch_facts_parses_fenced_json(monkeypatch):
    """模型偶爾會把 JSON 包在 ```json fence 裡。"""
    body = '```json\n{"events": [{"claim": "A", "date": "2026-08-05", "source": "s"}]}\n```'
    monkeypatch.setattr(
        retrieval.gemini_client, "generate_text_with_candidate",
        lambda *a, **k: (body, {"input_tokens": 5}, _chunks("https://a.test")),
    )
    pack, usage = retrieval.fetch_facts("2026-08-06")
    assert [e.claim for e in pack.events] == ["A"]
    assert pack.events[0].url == "https://a.test"
    assert usage["input_tokens"] == 5


@pytest.mark.parametrize("banned", ["分析", "選股", "推薦", "預測"])
def test_retrieval_rules_forbid_analysis(banned):
    """召回層 prompt 必須明文禁止做分析/選股——那是決策層的工作。"""
    assert banned in retrieval._RETRIEVAL_RULES
