"""防未來洩漏測試：training_set 的標籤/特徵時間對齊鐵律。

覆蓋 WP0.1 的第 1/2/3/5 組。每個測試都附「若把時間方向弄反會如何」的斷言，
確保這些測試對真正的洩漏敏感（見各測試 docstring 的「打破會 fail」說明）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from reports import training_set as ts


# ── 第 1 組：_triple_barrier 只掃 [t+1, t+h]、同日雙觸取下界、窗未滿 NaN ──

def test_triple_barrier_window_is_strictly_forward_and_bounded():
    """上界事件只在 t+h 之後發生時，t 不得偵測到（證明掃描窗上界＝t+h，不看更遠）。

    打破會 fail：若把掃描窗改成 range(t+1, n)（不設 t+h 上界），t=2 會看到 index 8 的
    up-spike，res 由 0 變 1，assert res[2]==0 立即紅掉。
    """
    n, h = 12, 5
    close = np.full(n, 100.0)
    high = np.full(n, 100.0)
    low = np.full(n, 100.0)
    high[8] = 110.0                                   # index 8 出現 +10% 的向上突破
    vol = np.full(n, 2.0)                             # 日波動 2% → volh = 2*sqrt(5) ≈ 4.47%

    out = ts._triple_barrier(close, high, low, vol, h)

    assert out[3] == 1          # 窗 [4,8] 含 index 8 → 觸上界
    assert out[2] == 0          # 窗 [3,7] 不含 index 8 → 兩界皆未觸
    assert np.all(np.isnan(out[n - h:]))             # 最後 h 筆窗未滿 → NaN


def test_triple_barrier_same_day_double_touch_takes_lower_bound():
    """同一根同時觸上下界時保守取下界（-1），對齊 backtest.evaluate_item 觸價優先序。

    打破會 fail：若把「下界優先」改成先檢查上界，res 會變 +1，assert == -1 紅掉。
    """
    n, h = 8, 3
    close = np.full(n, 100.0)
    high = np.full(n, 100.0)
    low = np.full(n, 100.0)
    high[2] = 110.0             # 同一根同時 +10% / -10%
    low[2] = 90.0
    vol = np.full(n, 2.0)       # volh = 2*sqrt(3) ≈ 3.46% → 兩界都被 index 2 觸發

    out = ts._triple_barrier(close, high, low, vol, h)
    assert out[0] == -1         # 窗 [1,3] 於 index 2 同觸 → 取下界


# ── 第 2 組：前瞻標籤（fwd_return / fwd_mae / fwd_vol）嚴格用未來，特徵不看未來 ──

def _smooth_closes(n: int) -> np.ndarray:
    idx = np.arange(n)
    return 100.0 + idx * 0.5 + np.sin(idx)           # 確定性、非常數 → 特徵非平凡


def test_forward_labels_use_future_features_use_past(install_symbol, price_factory):
    """在 t=25 之後追加一根極端未來 bar：t=25 的『特徵』完全不變，只有『標籤』改變。

    打破會 fail：若把 close.shift(-h) 誤成 shift(h)，fwd_return_5[25] 會用 close[20] 而非
    未來的 close[30]，approx 斷言紅掉；且 A 版該筆會從 NaN 變有值，第一個斷言也紅掉。
    """
    closes_b = _smooth_closes(31)
    closes_b[30] = 300.0                              # 未來極端跳空（僅 B 版有 index 30）
    closes_a = closes_b[:30].copy()                   # A 版少最後一根未來 bar
    highs_b, lows_b = closes_b + 1.0, closes_b - 1.0

    install_symbol("A", price_factory(closes_a, highs=highs_b[:30], lows=lows_b[:30]))
    fa = ts._symbol_long("A", [5])
    install_symbol("B", price_factory(closes_b, highs=highs_b, lows=lows_b))
    fb = ts._symbol_long("B", [5])

    feat_cols = ["return_1d_pct", "return_5d_pct", "return_20d_pct",
                 "volatility_20d_pct", "dist_ma20_pct", "turnover_surge"]
    for c in feat_cols:                               # 特徵只看過去 → A/B 在 t=25 必須逐位相同
        va, vb = fa.iloc[25][c], fb.iloc[25][c]
        assert va == pytest.approx(vb, nan_ok=True), c

    # 標籤看未來：A 版 t=25 無第 30 根 → NaN；B 版 = close[30]/close[25]-1（極端）
    assert np.isnan(fa.iloc[25]["fwd_return_5"])
    assert fb.iloc[25]["fwd_return_5"] == pytest.approx((300.0 / closes_b[25] - 1) * 100)
    # 風險家族前瞻標籤同理：窗未滿 → NaN；窗滿 → 有值
    assert np.isnan(fa.iloc[25]["fwd_mae_5"])
    assert np.isfinite(fb.iloc[25]["fwd_mae_5"])
    assert np.isnan(fa.iloc[25]["fwd_vol_5"])
    assert np.isfinite(fb.iloc[25]["fwd_vol_5"])


# ── 第 3 組：基本面 merge_asof(backward) point-in-time，不得取到公布日在後的財報 ──

def test_fundamentals_merge_asof_is_point_in_time(install_symbol, price_factory):
    """known_date 在 trade_date 之後的財報，絕不可出現在該 trade_date 的特徵。

    打破會 fail：若 merge_asof direction 改成 'forward'，2024-03-14 會取到公布日 03-15
    的 eps_ttm=99，assert == 10 紅掉。
    """
    fin = pd.DataFrame({
        "known_date": pd.to_datetime(["2024-02-01", "2024-03-15"]),
        "eps_ttm": [10.0, 99.0],
        "gross_margin_pct": [50.0, 60.0],
        "operating_margin_pct": [20.0, 25.0],
        "net_margin_pct": [15.0, 18.0],
        "debt_ratio_pct": [30.0, 35.0],
        "free_cashflow_ttm_100m": [1.0, 2.0],
    })
    install_symbol("X", price_factory(_smooth_closes(60)), fin=fin)
    f = ts._symbol_long("X", [5]).set_index("trade_date")

    def eps_on(date_str):
        return f.loc[pd.Timestamp(date_str), "eps_ttm"]

    assert np.isnan(eps_on("2024-01-10"))             # 早於首期公布日 → 無值
    assert eps_on("2024-03-14") == 10.0               # 取已公布(02-01)，不看未來(03-15)
    assert eps_on("2024-03-15") == 99.0               # 公布日當日起可見新一期


# ── 第 5 組：_overlap_weights 平均唯一性權重 = 1/重疊數，且不跨 (symbol,horizon) 群 ──

def test_overlap_weights_dilute_within_group():
    """同檔同窗、as_of 落在彼此前瞻窗內的樣本互相稀釋成 1/重疊數；孤立樣本權重 1.0。"""
    ds = pd.DataFrame({
        "symbol": ["AAA"] * 4,
        "horizon": [4] * 4,
        "as_of": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-06-01"],
    })
    w = ts._overlap_weights(ds)                       # 權重經 round(4) → 用 abs 容差比對
    assert w.iloc[0] == pytest.approx(1 / 3, abs=1e-3)   # 三筆群聚（span=6 日內）→ 1/3
    assert w.iloc[1] == pytest.approx(1 / 3, abs=1e-3)
    assert w.iloc[2] == pytest.approx(1 / 3, abs=1e-3)
    assert w.iloc[3] == pytest.approx(1.0)            # 6 月那筆孤立 → 1.0


def test_overlap_weights_do_not_cross_symbol():
    """不同 symbol 的同日樣本不得互相稀釋（證明有 group by symbol）。

    打破會 fail：若分群漏掉 symbol，六筆會併成一群 → count=6 → 權重 1/6，assert 1/3 紅掉。
    """
    ds = pd.DataFrame({
        "symbol": ["AAA", "AAA", "AAA", "BBB", "BBB", "BBB"],
        "horizon": [4] * 6,
        "as_of": ["2024-01-01", "2024-01-02", "2024-01-03"] * 2,
    })
    w = ts._overlap_weights(ds)
    assert all(w.iloc[i] == pytest.approx(1 / 3, abs=1e-3) for i in range(6))
