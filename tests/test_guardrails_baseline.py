"""spec 004 測試基線補齊：新聞 url 回填、缺 data_as_of 記 error、輸入不被改動、結果可重現。

其餘 guard（fail-closed、source_ref、禁語）見 tests/test_guardrails.py。
"""
from __future__ import annotations

from ai.schemas import BriefResult, BriefSection, Evidence, NewsDigestItem, WatchItem
from guardrails import verify

NEWS = [{"title": "台積電法說", "source": "cnyes", "date": "2026-06-01",
         "url": "https://news.example/tsmc", "tier": "authoritative"}]


def _features():
    return {"tw": {"stocks": {"2330": {"close": 900.0, "name": "台積電"}}}, "news": NEWS}


def _brief(**kw):
    base = dict(
        headline="這檔保證獲利", data_as_of="2026-06-02",
        sections=[BriefSection(title="台股", narrative="…", evidence=[
            Evidence(label="收盤", value="900", source_ref='tw.stocks["2330"].close'),
            Evidence(label="亂編", value="1", source_ref='tw.stocks["0000"].close'),
        ])],
        tw_watchlist=[WatchItem(symbol="2330", name="台積電", thesis="強"),
                      WatchItem(symbol="9999", name="不存在", thesis="捏造")],
        news_digest=[NewsDigestItem(title="台積電法說", source="cnyes", date="2026-06-01",
                                    takeaway="解讀"),                       # 沒帶 url
                     NewsDigestItem(title="虛構新聞", source="?", date="2026-06-01", takeaway="捏造")],
    )
    base.update(kw)
    return BriefResult(**base)


# ── T015 比對成功但缺 url → 回填、不丟；比對不到 → 移除 ──

def test_matched_news_without_url_is_backfilled_not_dropped():
    cleaned, report = verify.run_guardrails(_brief(), _features())
    assert [n.title for n in cleaned.news_digest] == ["台積電法說"]
    assert cleaned.news_digest[0].url == "https://news.example/tsmc"
    assert report["counts"]["news_dropped"] == 1


def test_news_matched_by_url_keeps_model_title():
    brief = _brief(news_digest=[NewsDigestItem(title="標題被模型改寫", source="cnyes", date="2026-06-01",
                                               url="https://news.example/tsmc", takeaway="t")])
    cleaned, _ = verify.run_guardrails(brief, _features())
    assert [n.title for n in cleaned.news_digest] == ["標題被模型改寫"]


# ── T017 禁語只標 warning 不刪；缺 data_as_of 記 error ──

def test_banned_phrase_is_warned_but_kept():
    cleaned, report = verify.run_guardrails(_brief(), _features())
    assert cleaned.headline == "這檔保證獲利"
    assert any(v["guard"] == "advice" and v["severity"] == "warning" for v in report["violations"])


def test_missing_data_as_of_is_an_error():
    brief = _brief().model_copy(update={"data_as_of": None})
    _, report = verify.run_guardrails(brief, _features())
    errs = [v for v in report["violations"] if v["guard"] == "data_age"]
    assert errs and errs[0]["severity"] == "error"
    assert report["passed"] is False


# ── T018 輸入物件不被改動；相同輸入 → 相同 report ──

def test_inputs_are_not_mutated():
    brief, feats = _brief(), _features()
    before_brief, before_feats = brief.model_dump(mode="json"), repr(feats)
    cleaned, _ = verify.run_guardrails(brief, feats)
    assert brief.model_dump(mode="json") == before_brief          # 捏造項仍在原物件上
    assert repr(feats) == before_feats
    assert cleaned is not brief and len(brief.tw_watchlist) == 2 and brief.news_digest[0].url is None


def test_same_input_gives_identical_report():
    a_clean, a_report = verify.run_guardrails(_brief(), _features())
    b_clean, b_report = verify.run_guardrails(_brief(), _features())
    a_report.pop("checked_at"), b_report.pop("checked_at")       # 只有檢查時間戳本來就該不同
    assert a_report == b_report
    assert a_clean.model_dump(mode="json") == b_clean.model_dump(mode="json")
