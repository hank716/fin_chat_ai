"""spec 003 測試基線：台股特徵（JSON-safe、單位、相對強弱、連買）、point-in-time、
基本面衍生指標、資料源缺失隔離、日曆感知快取、跨市場相關性對齊交易日。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from data_sources import finmind_loader
from processor import fundamentals as fu
from processor import intermarket_features as im
from processor import tw_features as tf
from storage import local_store


# ── 共用：把 tw_features 的讀取端全部導向記憶體 ──

def _prices(closes, start="2026-08-03", amount=1e8):
    days = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"trade_date": days, "open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": 1000, "amount": amount, "source": "test"})


def _chip(foreign, trust=None):
    n = len(foreign)
    return pd.DataFrame({"trade_date": pd.bdate_range("2026-08-03", periods=n),
                         "foreign_net_buy": foreign, "trust_net_buy": trust or [0] * n,
                         "dealer_net_buy": [0] * n, "source": "test"})


@pytest.fixture
def market(monkeypatch):
    data = {"prices": {}, "chip": {}, "margin": {}}
    empty_m = pd.DataFrame(columns=local_store.MARGIN_COLUMNS)
    empty_c = pd.DataFrame(columns=local_store.CHIP_COLUMNS)
    monkeypatch.setattr(local_store, "read_prices",
                        lambda s, m, adjusted=False: data["prices"].get(s, pd.DataFrame()).copy())
    monkeypatch.setattr(local_store, "read_chip", lambda s, m: data["chip"].get(s, empty_c).copy())
    monkeypatch.setattr(local_store, "read_margin", lambda s, m: data["margin"].get(s, empty_m).copy())
    monkeypatch.setattr(tf, "_available_symbols", lambda: set(data["prices"]))
    monkeypatch.setattr(tf.universe, "display_name", lambda s: f"名{s}")
    monkeypatch.setattr(tf.universe, "sector_of", lambda s: "電子")
    monkeypatch.setattr(tf.universe, "index_meta", lambda: {"name": "加權指數"})
    monkeypatch.setattr(fu, "build_fundamentals", lambda s: {"revenue_yoy_pct": 10.0})
    return data


# ── T012 JSON-safe、NaN→None、籌碼單位「張」──

def test_tw_features_are_json_safe_and_in_lots(market):
    closes = list(np.linspace(100, 120, 25))
    market["prices"]["TWII"] = _prices(list(np.linspace(1000, 1050, 25)))
    market["prices"]["2330"] = _prices(closes)
    market["prices"]["2603"] = _prices([np.nan] * 24 + [50.0])     # 大多 NaN 的新掛牌
    market["chip"]["2330"] = _chip([1_234_567, -500, 2_000_000, 3_000, 4_499])
    feats = tf.build_tw_features()
    json.dumps(feats, allow_nan=False)                              # 沒有 NaN/inf 漏出去
    s = feats["stocks"]["2330"]
    assert s["foreign_net_buy_1d_lots"] == 4                         # 4,499 股 → 4 張
    assert s["foreign_net_buy_5d_lots"] == round((1_234_567 - 500 + 2_000_000 + 3_000 + 4_499) / 1000)
    assert not any(k.startswith("_") for k in s)                     # 內部欄位不外流


# ── T013 相對強弱、連買/連賣天數 ──

@pytest.mark.parametrize("series,expected", [
    ([5, -1, 2, 3, 4], 3), ([1, 2, -3, -4], -2), ([1, 0], 0), ([], None), ([np.nan, 2], 1),
])
def test_net_streak(series, expected):
    assert tf._net_streak(pd.Series(series, dtype=float)) == expected


def test_relative_strength_is_stock_minus_index(market):
    market["prices"]["TWII"] = _prices([100.0] * 5 + [100 + i for i in range(1, 21)])   # 20 日 +20%
    market["prices"]["2330"] = _prices([100.0] * 5 + [100 + 2 * i for i in range(1, 21)])  # +40%
    s = tf.build_tw_features()["stocks"]["2330"]
    assert s["return_20d_pct"] == 40.0 and s["vs_index_20d_pct"] == 20.0


# ── T014 混入未來日列：寫入閘擋掉，特徵與 as_of 不受影響 ──

def test_future_rows_never_reach_features(tmp_path, monkeypatch):
    from data_sources.twse_loader import PriceRow
    monkeypatch.setattr(local_store, "PARQUET_ROOT", tmp_path)
    today = date.today()
    rows = [PriceRow(symbol="2330", trade_date=today - timedelta(days=i), open=100, high=100, low=100,
                     close=100 + i, volume=1, amount=1e8, source="twse") for i in range(30)]
    rows.append(PriceRow(symbol="2330", trade_date=today + timedelta(days=10), open=999, high=999,
                         low=999, close=999, volume=1, amount=1e8, source="twse"))
    local_store.write_prices(rows, "tw")
    block = tf._price_block(local_store.read_prices("2330", "tw"))
    assert block["as_of"] == today.isoformat() and block["close"] == 100.0


# ── T015 基本面衍生指標 ──

def test_revenue_yoy_mom(monkeypatch):
    rows = [{"revenue_year": 2025, "revenue_month": 9, "revenue": 80e8},
            {"revenue_year": 2026, "revenue_month": 8, "revenue": 90e8},
            {"revenue_year": 2026, "revenue_month": 9, "revenue": 100e8}]
    monkeypatch.setattr(finmind_loader, "get_month_revenue", lambda s, start: rows)
    assert fu._fetch_revenue("2330") == {"revenue_month": "2026-09", "revenue_100m": 100.0,
                                         "revenue_yoy_pct": 25.0, "revenue_mom_pct": 11.1}


def _long(date_, **items):
    return [{"date": date_, "type": k, "value": v} for k, v in items.items()]


@pytest.fixture
def statements(monkeypatch):
    fs = (_long("2025-12-31", EPS=2.0) + _long("2026-03-31", EPS=2.5)
          + _long("2025-09-30", EPS=1.5) + _long("2025-06-30", EPS=1.0)
          + _long("2026-06-30", Revenue=1000, GrossProfit=550, OperatingIncome=420,
                  IncomeAfterTaxes=380, EPS=3.0))
    bs = _long("2026-06-30", TotalAssets=2000, Liabilities=600, CurrentLiabilities=300)
    cf = []
    for d in ("2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"):
        cf += _long(d, CashFlowsFromOperatingActivities=5e8, PropertyAndPlantAndEquipment=-2e8)
    dv = [{"date": "2026-06-15", "CashEarningsDistribution": "4.5", "StockEarningsDistribution": ""}]
    calls = {"get_financial_statements": fs, "get_balance_sheet": bs, "get_cash_flows": cf,
             "get_dividend": dv}
    for name, rows in calls.items():
        monkeypatch.setattr(finmind_loader, name, lambda s, start, _r=rows: _r)
    return calls


def test_financial_derived_metrics(statements):
    out = fu._fetch_financials("2330")
    assert out["fiscal_quarter"] == "2026Q2"
    assert (out["gross_margin_pct"], out["operating_margin_pct"], out["net_margin_pct"]) == (55.0, 42.0, 38.0)
    assert out["eps_quarter"] == 3.0 and out["eps_ttm"] == 9.0      # 1.5+2.0+2.5+3.0
    assert out["debt_ratio_pct"] == 30.0                              # 用整體負債，不是流動負債
    assert out["op_cashflow_ttm_100m"] == 20.0 and out["free_cashflow_ttm_100m"] == 12.0
    assert out["dividend"] == {"year": "2026", "cash_per_share": 4.5, "stock_per_share": 0.0}


# ── T016 某資料源缺 → 只略過該區塊 ──

def test_one_statement_failure_keeps_the_rest(statements, monkeypatch):
    def boom(*_a):
        raise RuntimeError("balance sheet endpoint down")

    monkeypatch.setattr(finmind_loader, "get_balance_sheet", boom)
    out = fu._fetch_financials("2330")
    assert "debt_ratio_pct" not in out
    assert out["gross_margin_pct"] == 55.0 and out["eps_ttm"] == 9.0 and "dividend" in out


def test_finmind_backoff_only_drops_fundamentals_not_stocks(market, monkeypatch):
    """FinMind 退避時，focus 股照樣列出、只是缺基本面——不能整筆從 stocks 消失
    （消失的標的會被 guardrail 當成資料範圍外而從候選移除）。"""
    market["prices"]["TWII"] = _prices([100.0] * 25)
    for i, sym in enumerate(("1101", "1102", "1103", "1104")):
        market["prices"][sym] = _prices([100.0] * 20 + [100.0 + i + 1] * 5)

    def backoff(sym):
        raise finmind_loader.FinMindQuotaExceeded("402")

    monkeypatch.setattr(fu, "build_fundamentals", backoff)
    feats = tf.build_tw_features()
    focus = {it["symbol"] for lst in feats["movers"].values() for it in lst}
    assert set(feats["stocks"]) == focus
    assert all("fundamentals" not in e for e in feats["stocks"].values())


# ── T017 日曆感知快取：同期別不重抓、新一期理應公布才重抓 ──

@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fu, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(fu.settings, "fundamentals_filing_buffer_days", 0)
    return tmp_path


def test_cache_hit_does_not_refetch(cache):
    calls = []
    fetch = lambda: calls.append(1) or {"revenue_month": "2099-01"}   # noqa: E731
    fu._cached("2330", "revenue", 30, fetch)
    fu._cached("2330", "revenue", 30, fetch)
    assert len(calls) == 1


def test_old_cache_with_latest_period_is_still_fresh():
    target = fu._latest_expected_quarter(date.today(), 0)
    assert fu._section_fresh("financials", {"fiscal_quarter": target}, age_days=999, ttl_days=30)


def test_new_period_due_triggers_refetch_even_within_ttl():
    assert not fu._section_fresh("financials", {"fiscal_quarter": "2000Q1"}, age_days=1, ttl_days=30)


def test_negative_cache_uses_its_own_ttl(monkeypatch):
    monkeypatch.setattr(fu.settings, "fundamentals_negcache_ttl_days", 7)
    assert fu._section_fresh("revenue", None, age_days=6, ttl_days=1)
    assert not fu._section_fresh("revenue", None, age_days=8, ttl_days=1)


@pytest.mark.parametrize("today,expected", [
    (date(2026, 5, 14), "2025Q4"), (date(2026, 5, 15), "2026Q1"),
    (date(2026, 8, 14), "2026Q2"), (date(2026, 3, 30), "2025Q3"),
])
def test_latest_expected_quarter_follows_filing_deadlines(today, expected):
    assert fu._latest_expected_quarter(today, 0) == expected


# ── T018 跨市場相關性：先對齊共同交易日、再算報酬 ──

def test_correlation_returns_are_computed_on_aligned_days(monkeypatch):
    """BTC 週末也有價；若先各自算日報酬再對齊，週一那筆會是 BTC「週日→週一」對美股「週五→週一」。
    FR-007：對齊後再算報酬——兩邊的週一報酬都必須是「週五→週一」。"""
    days = pd.date_range("2026-09-01", periods=28, freq="D")        # 連續 4 週含週末
    rng = np.random.default_rng(0)
    btc_close = 100 * np.cumprod(1 + rng.normal(0, 0.02, len(days)))
    btc = pd.DataFrame({"trade_date": days, "close": btc_close})
    weekdays = days[days.dayofweek < 5]
    nasdaq = btc[btc["trade_date"].isin(weekdays)].reset_index(drop=True)   # 同價、只少週末
    frames = {"BTC": btc, "NASDAQ": nasdaq}
    monkeypatch.setattr(im, "ASSETS", [("NASDAQ", "us", "Nasdaq"), ("BTC", "crypto", "Bitcoin")])
    monkeypatch.setattr(im, "CORR_PAIRS", [("BTC", "NASDAQ")])
    monkeypatch.setattr(local_store, "read_prices",
                        lambda s, m, adjusted=False: frames[s].copy())
    corr = im.build_intermarket_features(window=15)["return_correlations_15d"]["BTC~NASDAQ"]
    assert corr == pytest.approx(1.0)                                # 對齊後同一條價格序列
