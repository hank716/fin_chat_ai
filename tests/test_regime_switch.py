"""spec 025 研究：能否用「當下已知」的資訊事前辨識動能/反轉行情（regime_switch_study）。

合成逐日序列：漲幅榜的 20 日超額在動能/反轉兩種行情間輪替。驗證——
訊號只用落後 ≥h 天、已結算的資訊；方向與門檻只由前半段決定；切換策略在後半段才評估。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from reports import strategy_calibration as sc

H = 20


def _daily(n=600, block=120, seed=0, noise=0.5):
    rng = np.random.default_rng(seed)
    regime = np.array([1 if (i // block) % 2 == 0 else -1 for i in range(n)])   # 動能 / 反轉輪替
    gain = 3.0 * regime + rng.normal(0, noise, n)
    fbuy = 1.0 + rng.normal(0, noise, n)
    return pd.DataFrame({
        "date": [d.date().isoformat() for d in pd.bdate_range("2024-01-01", periods=n)],
        f"top_gainers_5d_{H}": gain,
        f"bull_current_{H}": (gain + fbuy) / 2,
        f"bull_no_gainers_{H}": fbuy,
        "mkt_trend_20d_pct": rng.normal(0, 3, n),                # 與行情無關的雜訊訊號
        "pc_oi_z20": rng.normal(0, 1, n),
    })


def test_trailing_signal_is_lagged_by_horizon():
    d = _daily()
    sig = sc._trailing_signal(d[f"top_gainers_5d_{H}"], h=H, lookback=60)
    # 第 i 天只能用到 i-H 以前（含）已結算的值：把 i-H+1 之後的值改掉，訊號不能變
    i = 300
    tampered = d[f"top_gainers_5d_{H}"].copy()
    tampered.iloc[i - H + 1:] = 999.0
    assert sc._trailing_signal(tampered, h=H, lookback=60).iloc[i] == pytest.approx(sig.iloc[i])
    assert sig.iloc[: H + 60 - 1].isna().all()                 # 前段資料不足 → NaN


def test_persistent_regimes_are_detected_and_switching_helps():
    out = sc.regime_switch_study(daily=_daily(), h=H, lookback=60)
    tr = out["signals"]["trailing_gainers"]
    assert tr["ic_all"] > 0.2            # 訊號落後 h+lookback/2≈50 天，行情切換處必然失準
    sw = tr["switch_test_half"]
    assert sw["switch_mean_pct"] > sw["always_include_mean_pct"]
    assert sw["switch_mean_pct"] > sw["never_include_mean_pct"]


def test_noise_signals_show_no_edge():
    out = sc.regime_switch_study(daily=_daily(), h=H, lookback=60)
    for name in ("mkt_trend", "pc_fear"):
        assert abs(out["signals"][name]["ic_all"]) < 0.15, name


def test_direction_and_threshold_come_from_first_half_only():
    d = _daily()
    out = sc.regime_switch_study(daily=d, h=H, lookback=60)
    rule = out["signals"]["trailing_gainers"]["rule"]
    assert rule["fitted_on"] == [d["date"].iloc[0], d["date"].iloc[len(d) // 2 - 1]]
    assert rule["direction"] in (1, -1)


def test_reports_non_overlapping_sample():
    out = sc.regime_switch_study(daily=_daily(), h=H, lookback=60)
    tr = out["signals"]["trailing_gainers"]
    assert tr["n_nonoverlap"] == pytest.approx(tr["n_days"] / H, abs=2)


# ── 多切分點滾動驗證（擴張視窗、embargo h）──

def test_rolling_folds_train_only_on_settled_past():
    out = sc.regime_switch_rolling(daily=_daily(), h=H, lookback=60, n_blocks=6)
    assert out["n_folds"] == 6
    for f in out["folds"]:
        assert f["train_last_idx"] < f["test_first_idx"] - H + 1      # 訓練最後一筆 ≤ 測試起點 − h
        assert f["direction"] in (1, -1)


def test_rolling_detects_persistent_relation():
    out = sc.regime_switch_rolling(daily=_daily(), h=H, lookback=60, n_blocks=6)
    s = out["summary"]
    # 整段全放/全不放時與靜態策略打平，所以看整體逐日差值而非各段勝場
    assert s["switch_minus_always_pct"] > 0 and s["switch_minus_always_t"] > 2
    assert s["switch_minus_never_pct"] > 0 and s["switch_minus_never_t"] > 2


def test_rolling_noise_target_shows_no_consistent_gain():
    rng = np.random.default_rng(9)
    d = _daily()
    d[f"top_gainers_5d_{H}"] = rng.normal(0, 1, len(d))              # 與任何訊號無關
    d[f"bull_no_gainers_{H}"] = rng.normal(0, 1, len(d))             # 虛無假設：兩種池子平均相同
    d[f"bull_current_{H}"] = (d[f"top_gainers_5d_{H}"] + d[f"bull_no_gainers_{H}"]) / 2
    out = sc.regime_switch_rolling(daily=d, h=H, lookback=60, n_blocks=6)
    assert abs(out["summary"]["switch_minus_always_t"] or 0) < 2.5


def test_rolling_reports_direction_stability():
    out = sc.regime_switch_rolling(daily=_daily(), h=H, lookback=60, n_blocks=6)
    s = out["summary"]
    assert s["direction_flips"] == sum(1 for a, b in zip(out["folds"], out["folds"][1:])
                                       if a["direction"] != b["direction"])
