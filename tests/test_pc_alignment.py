"""WP0.3：TAIFEX P/C 時間對齊——盤後公布不得半天前視（train/serve 同一規則）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from processor import market_regime
from reports import training_set as ts


# ── 訓練端：date-D 樣本用 D-1 的 P/C（merge_asof backward，不允許同日）──

def test_training_pc_uses_prior_trading_day():
    """date-D 樣本掛到的 P/C 必須是嚴格早於 D 的最近一筆（＝D-1），不得用 D 當日盤後值。

    打破會 fail：若允許同日匹配（allow_exact_matches=True＝洩漏），01-03 會拿到 2.0（當日）
    而非 1.0（前一日），assert == 1.0 紅掉。
    """
    big = pd.DataFrame({
        "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        "symbol": ["A", "A", "A"],
    })
    pc = pd.DataFrame({
        "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        "known_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        "pc_oi_ratio": [1.0, 2.0, 3.0], "pc_oi_z20": [0.1, 0.2, 0.3],
        "pc_vol_ratio": [0.5, 0.6, 0.7], "pc_oi_chg5": [0.0, 0.0, 0.0],
    })
    out = ts._attach_pc_features(big, pc).set_index("trade_date")
    assert pd.isna(out.loc["2024-01-02", "pc_oi_ratio"])       # 無更早 → NaN
    assert out.loc["2024-01-03", "pc_oi_ratio"] == 1.0         # 取 01-02（D-1）
    assert out.loc["2024-01-04", "pc_oi_ratio"] == 2.0         # 取 01-03（D-1）


def test_training_pc_empty_frame_fills_nan():
    big = pd.DataFrame({"trade_date": pd.to_datetime(["2024-01-02"]), "symbol": ["A"]})
    out = ts._attach_pc_features(big, pd.DataFrame())
    for c in market_regime.PC_FEATURES:
        assert c in out.columns and pd.isna(out[c]).all()


# ── serve 端：盤前只用「今日之前已公布」的 P/C（known_date < 今日）──

def _fake_pcr(dates, oi):
    n = len(dates)
    return pd.DataFrame({
        "trade_date": pd.to_datetime(dates), "known_date": pd.to_datetime(dates),
        "pc_vol_ratio": [0.5] * n, "pc_oi_ratio": list(oi),
        "put_vol": [1.0] * n, "call_vol": [1.0] * n, "put_oi": [1.0] * n, "call_oi": [1.0] * n,
    })


def test_serving_latest_pc_excludes_today(monkeypatch):
    """D+1 早上 07:30、parquet 已有 D 日 P/C → latest_pc_features 回 D 日值（不是 D-1、也不是今日）。

    打破會 fail：若拿掉 known_date<今日 過濾（回 tail(1)），混入的今日(01-04=9.0)會被取用，
    assert == 2.0 紅掉。
    """
    # 資料到 D=01-03；「今天」是 D+1=01-04 早上
    monkeypatch.setattr(market_regime.taifex_loader, "read_pcr",
                        lambda: _fake_pcr(["2024-01-02", "2024-01-03"], [1.0, 2.0]))
    out = market_regime.latest_pc_features(now="2024-01-04 07:30")
    assert out["pc_oi_ratio"] == 2.0                          # D（昨日盤後）

    # 若 parquet 誤混入「今日 01-04」的 P/C（盤前尚未公布），不得取用
    monkeypatch.setattr(market_regime.taifex_loader, "read_pcr",
                        lambda: _fake_pcr(["2024-01-02", "2024-01-03", "2024-01-04"], [1.0, 2.0, 9.0]))
    out2 = market_regime.latest_pc_features(now="2024-01-04 07:30")
    assert out2["pc_oi_ratio"] == 2.0                         # 仍取 D，不取今日 9.0


def test_serving_pc_empty_when_only_today(monkeypatch):
    """只有今日 P/C（盤前尚未真正可用）→ 回空 dict，不前視。"""
    monkeypatch.setattr(market_regime.taifex_loader, "read_pcr",
                        lambda: _fake_pcr(["2024-01-04"], [9.0]))
    assert market_regime.latest_pc_features(now="2024-01-04 07:30") == {}
