"""spec 014 測試基線：meta 把握度不改方向、風險標記/避雷排序、sizing 兩道 gate、恐慌降曝險、
sizing 淨 P&L 回測把分數對應到損益。

模型一律換成可預測的替身——這裡驗的是「分數怎麼被用」，模型本身的預測力由離線評估負責。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from ai.schemas import BriefResult, WatchItem
from reports import backtest
from reports import morning_brief as mb
from reports import strategy_calibration as sc
from reports import training_set as T


def _result(wl=("A", "B", "C"), cau=("X", "Y", "Z")):
    return BriefResult(
        headline="h", data_as_of=date(2026, 10, 14),
        tw_watchlist=[WatchItem(symbol=s, name=s, thesis="t") for s in wl],
        tw_caution=[WatchItem(symbol=s, name=s, thesis="t") for s in cau],
    )


FEATS = {"tw": {"stocks": {s: {"volatility_20d_pct": 2.0} for s in "ABCDEXYZ"}, "movers": {}}}


@pytest.fixture(autouse=True)
def _no_regime(monkeypatch):
    monkeypatch.setattr(T, "current_market_regime", lambda: {})


# ── T010 meta 標把握度，但不改方向、不重排 ──

def test_meta_scores_mark_conviction_without_reordering(monkeypatch):
    monkeypatch.setattr(sc, "score_meta", lambda cands: {"A": 0.2, "B": 0.9, "C": 0.5, "X": 0.7})
    r = _result()
    mb._apply_meta_scores(r, FEATS)
    assert [w.symbol for w in r.tw_watchlist] == ["A", "B", "C"]
    assert [w.symbol for w in r.tw_caution] == ["X", "Y", "Z"]
    assert [w.conviction_score for w in r.tw_watchlist] == [0.2, 0.9, 0.5]
    assert r.tw_caution[0].conviction_score == 0.7 and r.tw_caution[1].conviction_score is None


def test_meta_gate_closed_returns_nothing(tmp_path, monkeypatch):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"per_horizon": {str(h): {"holdout_auc": 0.51}
                                                for h in backtest.horizons()}}))
    monkeypatch.setattr(sc, "META_META_PATH", meta)
    monkeypatch.setattr(sc.settings, "meta_model_min_auc", 0.6)
    cands = [{"symbol": "A", "side": "watchlist", "stock_entry": {}}]
    assert sc.score_meta(cands) == {}                          # AUC 不過 gate 就不啟用


def test_meta_disabled_returns_nothing(monkeypatch):
    monkeypatch.setattr(sc.settings, "enable_meta_model", False)
    assert sc.score_meta([{"symbol": "A", "side": "watchlist", "stock_entry": {}}]) == {}


# ── T011 風險：偏多只標記不重排；避雷依風險由高到低 ──

def test_risk_marks_watchlist_and_sorts_caution(monkeypatch):
    monkeypatch.setattr(sc, "score_risk",
                        lambda cands: {"A": 0.8, "B": 0.1, "C": 0.4, "X": 0.2, "Y": 0.9, "Z": 0.5})
    r = _result()
    mb._apply_risk_scores(r, FEATS)
    assert [w.symbol for w in r.tw_watchlist] == ["A", "B", "C"]
    assert [w.risk_score for w in r.tw_watchlist] == [0.8, 0.1, 0.4]
    assert [w.symbol for w in r.tw_caution] == ["Y", "Z", "X"]


def test_risk_without_model_changes_nothing(monkeypatch):
    monkeypatch.setattr(sc, "score_risk", lambda cands: {})
    r = _result()
    mb._apply_risk_scores(r, FEATS)
    assert [w.symbol for w in r.tw_caution] == ["X", "Y", "Z"]
    assert all(w.risk_score is None for w in r.tw_watchlist)


# ── T012 sizing：兩道 gate 都沒過 → no-op；過 gate → 權重 × 曝險 ──

def test_sizing_noop_when_both_gates_closed(monkeypatch):
    monkeypatch.setattr(sc, "sizing_plan", lambda: None)
    monkeypatch.setattr(sc, "market_exposure_scalar", lambda: (1.0, {}))
    r = _result()
    weights, info = mb._apply_sizing(r, FEATS)
    assert weights == {} and info == {}
    assert all(w.size_weight is None for w in r.tw_watchlist)


def test_sizing_with_scheme_weights_by_meta_and_sums_to_one(monkeypatch):
    monkeypatch.setattr(sc, "sizing_plan", lambda: "meta")
    monkeypatch.setattr(sc, "market_exposure_scalar", lambda: (1.0, {}))
    r = _result(wl=("A", "B", "C", "D", "E"))
    for w, p in zip(r.tw_watchlist, (0.25, 0.25, 0.2, 0.15, 0.15)):   # 皆低於單檔上限 0.30
        w.conviction_score = p
    weights, _ = mb._apply_sizing(r, FEATS)
    assert weights == pytest.approx({"A": 0.25, "B": 0.25, "C": 0.2, "D": 0.15, "E": 0.15})
    assert sum(weights.values()) == pytest.approx(1.0)


def test_exposure_scalar_alone_scales_equal_weights(monkeypatch):
    monkeypatch.setattr(sc, "sizing_plan", lambda: None)
    monkeypatch.setattr(sc, "market_exposure_scalar", lambda: (0.7, {"fear_pct": 0.6}))
    r = _result()
    weights, info = mb._apply_sizing(r, FEATS)
    assert sum(weights.values()) == pytest.approx(0.7, abs=1e-3)   # 其餘 30% 為現金（逐檔四捨五入）
    assert len(set(weights.values())) == 1 and info == {"fear_pct": 0.6}


def test_size_weights_respect_single_name_cap():
    w = sc._size_weights([{"meta_p": 0.9}, {"meta_p": 0.05}, {"meta_p": 0.05}, {"meta_p": 0.0}],
                         "meta", max_w=0.4)
    assert max(w) <= 0.4 + 1e-9 and sum(w) == pytest.approx(1.0)


def test_inv_risk_shifts_weight_away_from_risky_names():
    w = sc._size_weights([{"risk_p": 0.9}, {"risk_p": 0.1}], "inv_risk", max_w=1.0)
    assert w[1] > w[0]


# ── T013 市場恐慌高 → 總曝險下降；gate 沒過就不動 ──

@pytest.fixture
def regime_gate(tmp_path, monkeypatch):
    path = tmp_path / "regime.json"
    monkeypatch.setattr(sc, "MARKET_REGIME_PATH", path)
    monkeypatch.setattr(sc.settings, "enable_taifex", True)
    monkeypatch.setattr(sc.settings, "sizing_max_reduction", 0.5)
    return path


def _fear(monkeypatch, value):
    from processor import market_regime
    monkeypatch.setattr(market_regime, "market_fear_score", lambda asof=None: value)


def test_high_fear_lowers_exposure_more_than_low_fear(regime_gate, monkeypatch):
    regime_gate.write_text(json.dumps({"gate_passed": True}))
    _fear(monkeypatch, 0.9)
    high, info = sc.market_exposure_scalar()
    _fear(monkeypatch, 0.1)
    low, _ = sc.market_exposure_scalar()
    assert high == pytest.approx(0.55) and low == pytest.approx(0.95)
    assert info["elevated"] is True


def test_exposure_untouched_when_gate_failed(regime_gate, monkeypatch):
    regime_gate.write_text(json.dumps({"gate_passed": False}))
    _fear(monkeypatch, 1.0)
    assert sc.market_exposure_scalar() == (1.0, {})


def test_exposure_untouched_when_fear_unavailable(regime_gate, monkeypatch):
    regime_gate.write_text(json.dumps({"gate_passed": True}))
    _fear(monkeypatch, None)
    assert sc.market_exposure_scalar() == (1.0, {})


# ── T014 淨 P&L 回測：分數被正確換成損益（meta 對、risk 反）──

class _ColumnModel:
    """把唯一留下的特徵欄直接當機率——讓「分數 → 權重 → 損益」的對應可手算。"""

    def fit(self, X, y, sample_weight=None):  # noqa: ARG002
        # meta 的特徵欄是 risk 特徵欄 + conviction；有 conviction 就用它，否則用唯一的 risk 特徵
        self.col = "conviction" if "conviction" in X.columns else X.columns[0]
        return self

    def predict_proba(self, X):
        p = np.clip(X[self.col].to_numpy(dtype=float), 0.0, 1.0)
        return np.column_stack([1 - p, p])


def test_sizing_backtest_maps_scores_to_pnl(tmp_path, monkeypatch):
    rng = np.random.default_rng(0)
    h = backtest.horizons()[0]
    risk_col = next(c for c in backtest.FEATURE_COLUMNS if c != "volatility_20d_pct")
    rows = []
    start = date(2025, 1, 1)
    for d in range(200):
        as_of = (start + timedelta(days=d)).isoformat()
        for ret in rng.permutation([4.0, 1.0, -1.0, -4.0]):
            row = {c: 0.0 for c in backtest.META_FEATURE_COLUMNS}
            row.update({
                "as_of": as_of, "horizon": h, "side": "watchlist", "weight": 1.0,
                "fwd_return_pct": ret, "volatility_20d_pct": 2.0,
                "meta_label": int(ret > 0), "risk_label": int(ret < 0),
                "conviction": 0.9 if ret > 0 else 0.1,         # meta 特徵：贏家高
                risk_col: 0.9 if ret < 0 else 0.1,             # risk 特徵：輸家高
            })
            rows.append(row)
    monkeypatch.setattr(T, "_build_big", lambda hs: pd.DataFrame({"x": [1]}))
    monkeypatch.setattr(T, "_emit_all", lambda big, hs, **k: pd.DataFrame(rows))
    monkeypatch.setattr(sc, "_new_model", _ColumnModel)
    monkeypatch.setattr(sc, "STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(sc, "SIZING_BACKTEST_PATH", tmp_path / "sizing.json")
    monkeypatch.setattr(sc.settings, "sizing_min_alpha", 0.0)

    out = sc.backtest_sizing(horizon=h, max_w=1.0)
    s = out["schemes"]
    assert out["ran"] and out["oos_days"] >= 20
    assert s["equal"]["gross_mean_pct"] == pytest.approx(0.0)   # 4+1−1−4 等權為 0
    assert s["meta"]["alpha_vs_equal_pct"] > 0                    # 加重贏家 → 贏等權
    assert s["inv_risk"]["alpha_vs_equal_pct"] > 0                # 避開輸家 → 贏等權
    assert s["meta"]["net_pct"]["cost0.4"] == pytest.approx(s["meta"]["gross_mean_pct"] - 0.4)
    assert out["best_scheme"] in {"meta", "inv_risk", "combo"}
    assert json.loads((tmp_path / "sizing.json").read_text(encoding="utf-8"))["best_scheme"] == out["best_scheme"]


def test_sizing_plan_reads_gate_and_respects_switch(tmp_path, monkeypatch):
    path = tmp_path / "sizing.json"
    monkeypatch.setattr(sc, "SIZING_BACKTEST_PATH", path)
    monkeypatch.setattr(sc.settings, "enable_sizing", True)
    assert sc.sizing_plan() is None                              # 沒有回測證據 → 等權
    path.write_text(json.dumps({"best_scheme": "meta"}))
    assert sc.sizing_plan() == "meta"
    monkeypatch.setattr(sc.settings, "enable_sizing", False)
    assert sc.sizing_plan() is None
