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


# ── 否定語境下的禁語（2026-08-07 實測誤判）────────────────────────────────

def test_banned_phrase_under_negation_is_not_a_violation():
    """「不代表必然上漲」是我們要的謹慎表述，攔它等於懲罰正確行為。"""
    from guardrails.verify import CAUSALITY_BANNED, _scan_phrases

    text = "費半強彈可能對隔日半導體族群帶來偏正面的參考，但不代表必然上漲。"
    assert _scan_phrases(text, CAUSALITY_BANNED) == []


def test_bare_banned_phrase_still_caught():
    from guardrails.verify import CAUSALITY_BANNED, _scan_phrases

    assert "必然上漲" in _scan_phrases("費半強彈，隔日半導體必然上漲。", CAUSALITY_BANNED)


def test_negated_once_but_asserted_elsewhere_is_still_caught():
    """同段若另有一處未被否定地使用，仍要攔——不能看第一次出現就放行。"""
    from guardrails.verify import CAUSALITY_BANNED, _scan_phrases

    text = "這不代表必然上漲。不過就技術面看，站上均線後必然上漲。"
    assert "必然上漲" in _scan_phrases(text, CAUSALITY_BANNED)


# ── Verification Guard（spec 023 FR-009）────────────────────────────────


class _Clue:
    """ClueOutcome 的最小替身（guardrail 只認 duck-type，不綁 reports 層的型別）。"""

    def __init__(self, url, outcome, claim=""):
        self.url, self.outcome, self.claim = url, outcome, claim


def test_unverified_source_is_dropped_from_news_and_sources():
    """未經查證的線索被當事實引用 → 擋下（憲章 III fail-closed）。"""
    url = "https://rumor.example/story"
    brief = _brief(
        news_digest=[NewsDigestItem(title="傳聞", source="某媒體", date="2026-08-31",
                                    url=url, takeaway="影響大")],
        sources=[url, "https://ok.example/a"],
    )
    features = _features(news=[{"title": "傳聞", "url": url, "source": "某媒體",
                                "date": "2026-08-31"}])
    cleaned, report = verify.run_guardrails(
        brief, features, clue_outcomes=[_Clue(url, "unchecked_budget")],
    )
    assert cleaned.news_digest == []
    assert cleaned.sources == ["https://ok.example/a"]
    assert report["counts"]["unverified_refs_dropped"] == 2
    assert report["passed"] is False


def test_confirmed_clue_is_not_blocked():
    """已核對的線索照常放行——誤擋正確內容比漏擋更難察覺。"""
    url = "https://ok.example/story"
    brief = _brief(
        news_digest=[NewsDigestItem(title="已查證", source="某媒體", date="2026-08-31",
                                    url=url, takeaway="有原文佐證")],
        sources=[url],
    )
    features = _features(news=[{"title": "已查證", "url": url, "source": "某媒體",
                                "date": "2026-08-31"}])
    cleaned, report = verify.run_guardrails(
        brief, features, clue_outcomes=[_Clue(url, "confirmed")],
    )
    assert len(cleaned.news_digest) == 1 and cleaned.sources == [url]
    assert report["counts"]["unverified_refs_dropped"] == 0


def test_unverified_claim_quoted_in_thesis_is_flagged_not_deleted():
    """文字重疊是啟發式判定：標示而非刪除，誤判時的代價才不會是靜默刪掉正確內容。"""
    claim = "美國商務部宣布對先進封裝設備實施新的出口管制"
    brief = _brief(
        tw_watchlist=[WatchItem(symbol="2330", name="台積電",
                                thesis="美國商務部宣布對先進封裝設備實施新的出口管制，短線有壓")],
        sections=[BriefSection(title="跨市場連動",
                               narrative="美國商務部宣布對先進封裝設備實施新的出口管制。",
                               evidence=[])],
    )
    cleaned, report = verify.run_guardrails(
        brief, _features(stocks={"2330": {"close": 900}}),
        clue_outcomes=[_Clue("https://x.example/1", "unchecked_unreachable", claim)],
    )
    assert cleaned.tw_watchlist[0].thesis.endswith("（含未經查證線索）")
    assert cleaned.sections[0].narrative.endswith("（含未經查證線索）")
    assert report["counts"]["unverified_refs_flagged"] == 2
    assert report["warning_count"] >= 2
    assert report["passed"] is True          # warning 不影響 passed


def test_short_overlap_does_not_trigger_flag():
    """中文沒有詞界：「台積電」這種短字串在任何一篇晨報都會命中，門檻低了會天天誤標。"""
    brief = _brief(
        tw_watchlist=[WatchItem(symbol="2330", name="台積電", thesis="台積電量能穩定")],
    )
    cleaned, report = verify.run_guardrails(
        brief, _features(stocks={"2330": {"close": 900}}),
        clue_outcomes=[_Clue("https://x.example/1", "unchecked_budget", "台積電傳出擴產")],
    )
    assert "未經查證" not in cleaned.tw_watchlist[0].thesis
    assert report["counts"]["unverified_refs_flagged"] == 0


def test_guard_is_noop_without_clue_outcomes():
    """沒帶查證結局時整條 guard 跳過——問答路徑與既有呼叫點不受影響。"""
    url = "https://any.example/x"
    brief = _brief(sources=[url])
    cleaned, report = verify.run_guardrails(brief, _features())
    assert cleaned.sources == [url]
    assert report["counts"]["unverified_refs_dropped"] == 0


def test_norm_url_matches_verification_stats():
    """兩份正規化規則是複寫的（層級關係不允許反向 import），漂移就會誤刪已查證來源。

    guard 拿到的 key 來自 `ClueOutcome.url`，那邊已經正規化過；這邊若少做一步（例如
    不去 `www.`），比對就會落空——落空的方向是「把已查證的當未查證」，會靜默刪內容。
    """
    from reports.verification_stats import normalize_url

    for raw in ("https://www.cnyes.com/news/id/1", "https://cnyes.com/news/id/1/",
                "HTTPS://News.Example.COM/a?b=1#frag", "https://x.example/p",
                "", None, "not-a-url/"):
        assert verify._norm_url(raw) == normalize_url(raw), raw


def test_www_variant_of_unverified_clue_is_still_blocked():
    """線索是 www、報告寫裸網域（或反過來）時，guard 不得因此漏擋。"""
    brief = _brief(sources=["https://cnyes.com/news/id/1"])
    cleaned, report = verify.run_guardrails(
        brief, _features(),
        clue_outcomes=[_Clue("https://www.cnyes.com/news/id/1", "unchecked_budget")],
    )
    assert cleaned.sources == []
    assert report["counts"]["unverified_refs_dropped"] == 1

