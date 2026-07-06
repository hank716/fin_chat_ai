"""防未來洩漏測試：backtest 前瞻窗嚴格性 + train/serve 特徵 parity。

覆蓋 WP0.1 的第 4/7 組。
"""
from __future__ import annotations

import pandas as pd
import pytest

from reports import backtest as bt


# ── 第 4 組：_forward_window 嚴格 trade_date > as_of（as_of 當日不得入窗）──

def test_forward_window_strictly_after_as_of(install_symbol, price_factory):
    """前瞻窗只含 trade_date 嚴格大於 as_of 的列，且長度上限 = n。

    打破會 fail：若把 `> as_of` 改成 `>= as_of`，as_of 當日(第 5 個交易日)會被含進窗，
    首列會等於 as_of、且 as_of 出現在結果中，兩個斷言都紅掉。
    """
    px = price_factory([100.0 + i for i in range(10)])
    install_symbol("S", px)
    as_of = px["trade_date"].iloc[4]                  # 第 5 個交易日當作資料日

    fwd = bt._forward_window("S", as_of, 3)

    assert len(fwd) == 3
    assert (fwd["trade_date"] > as_of).all()          # 全部嚴格在 as_of 之後
    assert as_of not in set(fwd["trade_date"])        # as_of 當日不得入窗
    assert fwd["trade_date"].iloc[0] == px["trade_date"].iloc[5]   # 首列 = 下一個交易日


def test_forward_window_caps_at_available(install_symbol, price_factory):
    """接近序列末端時，前瞻窗長度不超過實際可得的未來交易日數。"""
    px = price_factory([100.0 + i for i in range(10)])
    install_symbol("S", px)
    as_of = px["trade_date"].iloc[8]                  # 只剩 1 個未來交易日
    fwd = bt._forward_window("S", as_of, 5)
    assert len(fwd) == 1


# ── 第 7 組：featurize 對「平面欄位(訓練集)」與「巢狀 fundamentals(線上)」結果一致 ──

def test_featurize_flat_and_nested_fundamentals_parity():
    """同樣的基本面數值，不論放平面 key 或巢狀 fundamentals，featurize 輸出必須逐欄相同。

    打破會 fail：若移除對 stock_entry['fundamentals'] 的 fallback，nested 版的基本面欄
    會變 None，兩版不再相等，assert 紅掉（這正是 train/serve skew 的守門）。
    """
    base = {
        "return_1d_pct": 1.2, "return_5d_pct": 3.4, "return_20d_pct": -2.1,
        "volatility_20d_pct": 2.5, "dist_ma20_pct": 1.1, "dist_ma60_pct": 4.2,
        "vs_index_5d_pct": 0.5, "vs_index_20d_pct": -1.0, "sector_rs_20d_pct": 0.8,
        "foreign_net_buy_5d_lots": 120.0, "foreign_net_streak": 3,
        "trust_net_streak": 1, "dealer_net_buy_5d_lots": -5.0, "dealer_net_streak": -2,
        "turnover_surge": 1.8, "margin_chg_5d_lots": 10.0, "short_margin_ratio_pct": 5.0,
        "mkt_trend_20d_pct": 1.0, "mkt_vol_20d_pct": 1.5,
        "pc_oi_ratio": 1.1, "pc_oi_z20": 0.3, "pc_vol_ratio": 0.9, "pc_oi_chg5": 0.05,
    }
    fund = {
        "revenue_yoy_pct": 12.0, "revenue_mom_pct": 3.0, "eps_ttm": 5.5,
        "gross_margin_pct": 55.0, "operating_margin_pct": 22.0, "net_margin_pct": 18.0,
        "debt_ratio_pct": 30.0, "free_cashflow_ttm_100m": 1.4,
    }
    flat = {**base, **fund}
    nested = {**base, "fundamentals": fund}

    out_flat = bt.featurize(flat, "watchlist")
    out_nested = bt.featurize(nested, "watchlist")

    assert out_flat == out_nested
    assert out_flat["side_bull"] == 1.0
    assert bt.featurize(flat, "caution")["side_bull"] == 0.0
    # 基本面欄確實被讀進來（非全 None）
    assert out_nested["eps_ttm"] == 5.5
    assert out_nested["gross_margin_pct"] == 55.0
