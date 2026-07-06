"""WP3.5：guardrail 高風險路徑——symbol fail-closed、source_ref 解析、新聞比對、禁語。"""
from __future__ import annotations

from ai.schemas import BriefResult, BriefSection, Evidence, NewsDigestItem, WatchItem
from guardrails import verify


def _features(stocks=None, news=None):
    return {
        "tw": {"stocks": stocks if stocks is not None else {}},
        "news": news or [],
    }


def _brief(**kw):
    base = dict(headline="今日中性", data_as_of="2026-06-02")
    base.update(kw)
    return BriefResult(**base)


# ── _resolve：容忍 JSONPath 方言，但捏造路徑一律 _MISSING ──

def test_resolve_dialects_and_missing():
    feats = {"tw": {"stocks": {"2330": {"close": 900.0}}}, "us_crypto": {"assets": {"SOX": {"r": 22}}}}
    assert verify._resolve(feats, "tw.stocks[\"2330\"].close") == 900.0
    assert verify._resolve(feats, "features.tw.stocks[\"2330\"].close") == 900.0   # 省略根前綴
    assert verify._resolve(feats, "$.us_crypto.assets.SOX.r") == 22                # $ 前綴
    assert verify._resolve(feats, "tw.stocks[\"2330\"].close（收盤）") == 900.0     # 截黏著註解
    assert verify._resolve(feats, "tw.stocks[\"9999\"].close") is verify._MISSING  # 不存在
    assert verify._resolve(feats, "tw.bogus.path") is verify._MISSING


# ── Symbol Guard：fail-closed ──

def test_symbol_guard_fail_closed_when_stocks_empty():
    """stocks 為空（台股資料失敗）→ 所有候選一律移除，而非放行。"""
    brief = _brief(
        tw_watchlist=[WatchItem(symbol="2330", name="台積電", thesis="強")],
        tw_caution=[WatchItem(symbol="2317", name="鴻海", thesis="弱")],
    )
    cleaned, report = verify.run_guardrails(brief, _features(stocks={}))
    assert cleaned.tw_watchlist == [] and cleaned.tw_caution == []
    assert report["counts"]["symbols_dropped"] == 2
    assert report["passed"] is False


def test_symbol_guard_keeps_known_drops_unknown():
    brief = _brief(tw_watchlist=[
        WatchItem(symbol="2330", name="台積電", thesis="在範圍"),
        WatchItem(symbol="9999", name="幽靈", thesis="不在範圍"),
    ])
    cleaned, report = verify.run_guardrails(brief, _features(stocks={"2330": {"close": 900}}))
    kept = [w.symbol for w in cleaned.tw_watchlist]
    assert kept == ["2330"]
    assert report["counts"]["symbols_dropped"] == 1


# ── Source / Metric Guard ──

def test_metric_guard_drops_fabricated_source_ref():
    brief = _brief(sections=[BriefSection(title="台股大盤", narrative="…", evidence=[
        Evidence(label="收盤", value="900", source_ref="tw.stocks[\"2330\"].close"),   # 真實
        Evidence(label="亂編", value="1", source_ref="tw.stocks[\"0000\"].close"),      # 捏造
    ])])
    cleaned, report = verify.run_guardrails(brief, _features(stocks={"2330": {"close": 900}}))
    refs = [e.source_ref for e in cleaned.sections[0].evidence]
    assert refs == ["tw.stocks[\"2330\"].close"]
    assert report["counts"]["evidence_dropped"] == 1


# ── News Citation Guard ──

def test_news_guard_drops_unmatched():
    news = [{"title": "台積電法說", "source": "cnyes", "date": "2026-06-01",
             "url": "http://x/1", "tier": "authoritative"}]
    brief = _brief(news_digest=[
        NewsDigestItem(title="台積電法說", source="cnyes", date="2026-06-01",
                       url="http://x/1", takeaway="解讀"),
        NewsDigestItem(title="虛構新聞", source="?", date="2026-06-01", takeaway="捏造"),
    ])
    cleaned, report = verify.run_guardrails(brief, _features(news=news))
    kept = [n.title for n in cleaned.news_digest]
    assert kept == ["台積電法說"]
    assert report["counts"]["news_dropped"] == 1


# ── Advice / Causality 禁語（warning，不移除）──

def test_phrase_scan_warns_not_removes():
    brief = _brief(headline="這檔保證獲利、隔日一定漲")
    cleaned, report = verify.run_guardrails(brief, _features())
    assert cleaned.headline == "這檔保證獲利、隔日一定漲"           # 敘事不因禁語被刪
    assert report["counts"]["phrase_warnings"] >= 1
    guards = {v["guard"] for v in report["violations"]}
    assert "advice" in guards
