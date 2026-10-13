"""spec 024：小型股候選——模型直接出名單、四道關卡、與主名單隔離。"""
from __future__ import annotations

import pytest

from ai import prompts
from config import settings
from data_sources import trading_restrictions
from processor import tw_features
from reports import backtest, markdown_builder, smallcap_sleeve
from reports import strategy_calibration as sc
from reports import training_set


def _entry(ret5, foreign, amount, sector="電子零組件業", **extra):
    return {"name": "N", "sector": sector, "close": 20.0, "return_5d_pct": ret5,
            "return_20d_pct": 1.0, "foreign_net_buy_5d_lots": foreign, "dist_ma20_pct": 2.0,
            "_amount": amount, **extra}


@pytest.fixture
def no_fundamentals(monkeypatch):
    import processor.fundamentals as f
    monkeypatch.setattr(f, "build_fundamentals", lambda s: None)


def test_pool_is_band_limited_and_matches_backtest_rule(no_fundamentals, monkeypatch):
    """帶內的 5 日漲幅榜 ∪ 外資買超榜（各前 N）；帶外與 ETF 一律不進。"""
    monkeypatch.setattr(settings, "smallcap_min_amount_twd", 10_000_000)
    stocks = {
        "1001": _entry(9.0, 10, 20e6),             # 漲幅榜
        "1002": _entry(-3.0, 900, 30e6),           # 外資買超榜
        "1003": _entry(1.0, 1, 40e6),              # 兩榜都排不進前 1
        "1004": _entry(50.0, 5000, 5e6),           # 低於下限
        "1005": _entry(50.0, 5000, 80e6),          # 屬主池
        "0050": _entry(50.0, 5000, 20e6, sector="ETF"),
    }
    pool = tw_features._smallcap_pool(stocks, top=1)
    assert sorted(pool["stocks"]) == ["1001", "1002"]
    assert pool["band_twd"] == [10_000_000.0, float(tw_features.MIN_AMOUNT_TWD)]
    entry = pool["stocks"]["1001"]
    assert entry["amount_twd"] == 20e6 and not any(k.startswith("_") for k in entry)


def test_pool_never_reaches_the_decision_prompt():
    feats = {"tw": {"stocks": {"2330": {}}, "smallcap": {"stocks": {"1001": {}}}}, "news": []}
    assert "smallcap" not in prompts._budget_features(feats)["tw"]
    assert "smallcap" in feats["tw"]                      # 原物件不被改動（落地報告仍要用）


@pytest.fixture
def wired(monkeypatch):
    """預設：功能開、名單可用且無人受限、模型過門檻並以 return_5d 當分數。"""
    monkeypatch.setattr(settings, "enable_smallcap_candidates", True)
    monkeypatch.setattr(settings, "smallcap_top_k", 3)
    monkeypatch.setattr(trading_restrictions, "restricted_symbols", lambda: set())
    monkeypatch.setattr(training_set, "current_market_regime", lambda: {})
    monkeypatch.setattr(smallcap_sleeve, "live_gate", lambda stats=None: (True, None))
    monkeypatch.setattr(sc, "_score_rank_band", lambda cands, band: {
        c["symbol"]: c["stock_entry"]["return_5d_pct"] for c in cands} if band == "smallcap" else {})
    monkeypatch.setattr(sc, "score_risk", lambda cands: {c["symbol"]: 0.7 for c in cands})


def _tw(n=5):
    stocks = {f"10{i:02d}": {"name": f"S{i}", "sector": "X", "close": 10.0 + i,
                             "return_5d_pct": float(i), "return_20d_pct": 2.0,
                             "foreign_net_buy_5d_lots": -120 * i, "dist_ma20_pct": 1.5,
                             "amount_twd": 2e7} for i in range(n)}
    return {"smallcap": {"band_twd": [1e7, 5e7], "stocks": stocks}}


def test_model_picks_top_k_in_score_order(wired):
    out = smallcap_sleeve.build(_tw())
    assert [p["symbol"] for p in out["picks"]] == ["1004", "1003", "1002"]
    assert [p["rank"] for p in out["picks"]] == [1, 2, 3]
    assert out["reason"] is None and out["pool_n"] == 5
    top = out["picks"][0]
    assert top["risk_score"] == 0.7 and top["amount_twd"] == 2e7
    # 訊號由資料直接組出，數字與 features 一致、不經語言模型
    assert "5日報酬 +4.00%" in top["signals"] and "外資5日賣超480張" in top["signals"]
    assert "日成交額 2,000 萬元" in top["signals"]


def test_restricted_stocks_are_excluded(wired, monkeypatch):
    monkeypatch.setattr(trading_restrictions, "restricted_symbols", lambda: {"1004", "9999"})
    out = smallcap_sleeve.build(_tw())
    assert [p["symbol"] for p in out["picks"]] == ["1003", "1002", "1001"]
    assert out["excluded_restricted"] == ["1004"]


@pytest.mark.parametrize("break_it, expect", [
    (lambda mp: mp.setattr(settings, "enable_smallcap_candidates", False), "未啟用"),
    (lambda mp: mp.setattr(trading_restrictions, "restricted_symbols", lambda: None), "名單無法取得"),
    (lambda mp: mp.setattr(sc, "_score_rank_band", lambda c, b: {}), "未通過門檻"),
    (lambda mp: mp.setattr(smallcap_sleeve, "live_gate", lambda stats=None: (False, "停損")), "停損"),
])
def test_any_failed_gate_means_no_list_with_a_reason(wired, monkeypatch, break_it, expect):
    """四道關卡任一不過就不列，而且要說得出原因——不得靜默、也不得放行部分結果。"""
    break_it(monkeypatch)
    out = smallcap_sleeve.build(_tw())
    assert out["picks"] == [] and expect in out["reason"]


def test_empty_pool_is_not_backfilled(wired):
    out = smallcap_sleeve.build({"smallcap": {"stocks": {}}})
    assert out["picks"] == [] and "沒有符合條件" in out["reason"]


def _card(*excess):
    return {"smallcap_items": [{"horizons": {"5": {"vs_index_pct": v}}} for v in excess]}


def test_live_stats_are_day_weighted(monkeypatch):
    monkeypatch.setattr(backtest, "horizons", lambda: [5, 20])
    stats = smallcap_sleeve.live_stats([_card(3.0, 1.0), _card(-4.0), {"items": []}])
    assert stats["n_days"] == 2 and stats["n_picks"] == 3
    assert stats["avg_excess_vs_market_pct"] == pytest.approx(-1.0)     # (2 + -4) / 2
    assert stats["beat_market_day_rate"] == 0.5
    assert smallcap_sleeve.live_stats([]) == {"n_days": 0, "n_picks": 0}


def test_live_gate_waits_for_evidence_then_stops_on_negative():
    losing = {"n_days": smallcap_sleeve.LIVE_MIN_DAYS, "avg_excess_vs_market_pct": -0.8}
    assert smallcap_sleeve.live_gate(losing)[0] is False
    assert smallcap_sleeve.live_gate({**losing, "n_days": 19})[0] is True          # 樣本不足先放行
    assert smallcap_sleeve.live_gate({**losing, "avg_excess_vs_market_pct": 0.3})[0] is True
    assert smallcap_sleeve.live_gate({"n_days": 0, "n_picks": 0})[0] is True


def test_scorecard_keeps_smallcap_out_of_main_items(monkeypatch):
    """校準與模型訓練會掃 items 並把非 watchlist 的當偏空——小型股必須放在另一個鍵。"""
    import pandas as pd

    fwd = pd.DataFrame({"high": [11.0] * 5, "low": [9.5] * 5, "close": [10.0, 10.2, 10.4, 10.6, 11.0]})
    monkeypatch.setattr(backtest, "_forward_window", lambda sym, as_of, n, adjusted=False: fwd.head(n))
    monkeypatch.setattr(backtest, "_entry_close_adj", lambda sym, as_of: 10.0)
    monkeypatch.setattr(backtest, "_index_forward_return", lambda as_of, n: 4.0)
    monkeypatch.setattr(backtest, "_data_matured", lambda as_of, h: h == 5)
    report = {"report_id": "r", "features": {"as_of": "2026-10-12", "tw": {
        "stocks": {}, "smallcap": {"stocks": {"1001": {"close": 10.0, "name": "S"}}}}},
        "tw_watchlist": [], "tw_caution": [],
        "tw_smallcap": {"picks": [{"symbol": "1001", "name": "S", "rank": 1, "rank_score": 0.9}]}}
    card = backtest.evaluate_report(report, hs=[5, 20])
    assert card["items"] == []
    item = card["smallcap_items"][0]
    assert item["horizons"]["5"]["forward_return_pct"] == 10.0
    assert item["horizons"]["5"]["vs_index_pct"] == 6.0
    assert item["horizons"]["20"]["outcome"] == "pending"


def test_markdown_states_the_reason_when_nothing_is_listed():
    from datetime import date

    from ai.schemas import BriefResult

    result = BriefResult(headline="h", data_as_of=date(2026, 10, 12))
    listed = markdown_builder.build_markdown(result, smallcap={
        "note": "低流動性。", "reason": None,
        "picks": [{"rank": 1, "symbol": "1001", "name": "S", "sector": "X",
                   "signals": ["5日報酬 +4.00%"], "risk_score": 0.7}]})
    assert "小型股候選" in listed and "1. 1001 S" in listed and "回撤風險偏高（70%）" in listed
    empty = markdown_builder.build_markdown(result, smallcap={"picks": [], "reason": "模型未通過門檻"})
    assert "今日不列：模型未通過門檻" in empty
    assert "小型股候選" not in markdown_builder.build_markdown(result)


def test_restriction_list_fails_closed(monkeypatch):
    """任一來源失敗就回 None，不回部分名單。"""
    class _Client:
        def __init__(self, *a, **k): self.n = 0
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("boom")
            return type("R", (), {"raise_for_status": lambda s: None,
                                  "json": lambda s: [{"Code": "1234"}]})()

    monkeypatch.setattr(trading_restrictions.httpx, "Client", _Client)
    assert trading_restrictions.restricted_symbols() is None
