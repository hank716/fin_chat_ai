"""spec 025 Step 0：候選池回測（backtest_candidate_pools）——逐榜與各候選池變體的橫斷面超額。

用可手算的合成橫斷面：漲幅榜之後必跌、外資買超榜之後必漲、高波動者跌，
確認「名單 → 超額」的對應、池子變體的組成、risk 過濾的方向都正確。
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from reports import strategy_calibration as sc
from reports import training_set as T

N_SYMS, N_DAYS = 40, 150


def _big(hs=(5, 20), seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for d, day in enumerate(pd.bdate_range("2024-01-01", periods=N_DAYS)):
        ret5 = rng.normal(0, 5, N_SYMS)
        fbuy = rng.normal(0, 1000, N_SYMS)
        gainers = set(np.argsort(-ret5)[:T._TOP])
        fbuyers = set(np.argsort(-fbuy)[:T._TOP])
        for i in range(N_SYMS):
            fwd = 0.0
            if i in gainers:
                fwd -= 4.0                                   # 追漲幅榜＝之後反轉
            if i in fbuyers:
                fwd += 2.0                                   # 外資買超＝之後續強
            vol = 5.0 if fwd < 0 else 1.0                    # 會跌的那些波動高（risk 模型抓得到）
            row = {"trade_date": day, "symbol": f"{1100 + i}", "sector": "電子", "amount": 1e8,
                   "return_5d_pct": ret5[i], "foreign_net_buy_5d_lots": fbuy[i],
                   "short_margin_ratio_pct": rng.uniform(0, 50), "vs_index_20d_pct": rng.normal(0, 3),
                   "volatility_20d_pct": vol}
            for h in hs:
                row.update({f"fwd_return_{h}": fwd, f"fwd_mae_{h}": -vol, f"fwd_vol_{h}": vol,
                            f"fwd_absmove_{h}": abs(fwd), f"fwd_tb_{h}": 0})
            rows.append(row)
    return pd.DataFrame(rows)


class _VolModel:
    """risk 替身：直接拿波動度當風險機率（高波動＝高風險）。"""

    def fit(self, X, y, sample_weight=None):  # noqa: ARG002
        return self

    def predict_proba(self, X):
        p = np.clip(X["volatility_20d_pct"].to_numpy(dtype=float) / 10.0, 0, 1)
        return np.column_stack([1 - p, p])


@pytest.fixture
def run(tmp_path, monkeypatch):
    big = _big()
    monkeypatch.setattr(T, "_build_big", lambda hs: big.copy())
    monkeypatch.setattr(sc, "_new_model", _VolModel)
    monkeypatch.setattr(sc, "STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(sc, "CANDIDATE_POOLS_PATH", tmp_path / "candidate_pools_backtest.json")
    return lambda: sc.backtest_candidate_pools(horizons=(5, 20))


def test_per_list_excess_matches_construction(run):
    out = run()
    assert out["ran"] and out["n_days"] == N_DAYS
    lists = out["lists"]
    # 每日中位數基準：40 檔裡 8 檔 −4、8 檔 +2（重疊者 −2），中位數為 0
    assert lists["top_gainers_5d"]["5"]["mean_excess_pct"] < -3.0
    assert lists["top_foreign_buy_5d"]["5"]["mean_excess_pct"] > 1.0
    assert lists["top_gainers_5d"]["20"]["t_stat"] < -10              # 合成資料幾乎無雜訊


def test_pool_variants_are_built_from_the_right_lists(run):
    pools = run()["pools"]
    current = pools["bull_current"]["5"]["mean_excess_pct"]
    no_gainers = pools["bull_no_gainers"]["5"]["mean_excess_pct"]
    assert no_gainers > current                                       # 拿掉漲幅榜，偏多池變好
    assert pools["bear_plus_gainers"]["5"]["mean_excess_pct"] < pools["bear_current"]["5"]["mean_excess_pct"]


def test_risk_filter_uses_out_of_sample_scores_and_helps(run):
    rf = run()["risk_filter"]["5"]
    assert rf["oos_days"] >= 20
    assert rf["filtered_mean_excess_pct"] > rf["unfiltered_mean_excess_pct"]


def test_result_is_written_and_reports_basis(run, tmp_path):
    out = run()
    saved = json.loads((tmp_path / "candidate_pools_backtest.json").read_text(encoding="utf-8"))
    assert saved["lists"] == out["lists"]
    assert "中位數" in out["note"]


def test_no_data_returns_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_build_big", lambda hs: None)
    monkeypatch.setattr(sc, "CANDIDATE_POOLS_PATH", tmp_path / "x.json")
    assert sc.backtest_candidate_pools()["ran"] is False


def test_baseline_is_pool_mean_not_median(tmp_path, monkeypatch):
    """右偏分布下平均數 > 中位數：若用中位數當基準，任何高波動名單都會「看起來」贏。
    這裡每檔報酬都是隨機、與名單無關——任何榜的平均超額都應趨近 0。"""
    rng = np.random.default_rng(1)
    big = _big()
    lognormal = rng.lognormal(0, 0.8, len(big)) * 10 - 12          # 強右偏、與名單無關
    for h in (5, 20):
        big[f"fwd_return_{h}"] = lognormal
    monkeypatch.setattr(T, "_build_big", lambda hs: big.copy())
    monkeypatch.setattr(sc, "_new_model", _VolModel)
    monkeypatch.setattr(sc, "STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(sc, "CANDIDATE_POOLS_PATH", tmp_path / "x.json")
    out = sc.backtest_candidate_pools(horizons=(5, 20))
    for name, v in out["lists"].items():
        assert abs(v["5"]["mean_excess_pct"]) < 1.0, name


def test_daily_stats_mean_and_t():
    m, t, n = sc._daily_stats([1.0, 2.0, 3.0])
    assert (m, n) == (2.0, 3) and t == pytest.approx(2.0 / (1.0 / np.sqrt(3)), abs=0.01)
    assert sc._daily_stats([]) == (None, None, 0)


def test_date_window_limits_days(run, tmp_path, monkeypatch):
    days = pd.bdate_range("2024-01-01", periods=N_DAYS)
    out = sc.backtest_candidate_pools(horizons=(5,), start=str(days[10].date()), end=str(days[39].date()))
    assert out["n_days"] == 30 and out["window"] == [str(days[10].date()), str(days[39].date())]
