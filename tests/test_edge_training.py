"""spec 013 測試基線：回放選股與線上 movers 同規則、樣本不足不訓練、edge serving 守護、precision@k。

訓練集若用和線上不同的規則選股，模型學到的就是另一個母體——這是最容易靜默走偏的地方。
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from processor import tw_features
from reports import backtest
from reports import strategy_calibration as sc
from reports import training_set as T


# ── T012 回放選股 ≡ 線上 movers（流動性門檻、排除 ETF、各榜 top N）──

def _universe(n=40, seed=3):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        rows.append({
            "symbol": f"{1100 + i}", "name": f"S{i}",
            "sector": "ETF" if i % 9 == 0 else ("受益證券" if i % 13 == 0 else "電子"),
            "amount": float(rng.choice([1e7, 6e7, 2e8])),      # 一部分低於 5,000 萬門檻
            "return_5d_pct": float(rng.normal(0, 5)),
            "foreign_net_buy_5d_lots": float(rng.normal(0, 1000)),
            "short_margin_ratio_pct": float(rng.uniform(0, 60)),
            "vs_index_20d_pct": float(rng.normal(0, 4)),
        })
    return rows


def test_replay_selection_matches_live_movers():
    rows = _universe()
    live = tw_features._movers({r["symbol"]: {**r, "_amount": r["amount"]} for r in rows},
                               top=T._TOP)
    live_bull = {it["symbol"] for k in ("top_gainers_5d", "top_foreign_buy_5d") for it in live[k]}
    live_bear = {it["symbol"] for k in ("top_losers_5d", "top_foreign_sell_5d",
                                        "top_short_margin_ratio", "top_below_index_20d")
                 for it in live[k]}

    day = pd.DataFrame(rows)
    day["trade_date"] = pd.Timestamp("2026-09-01")
    h = 5
    for prefix in ("fwd_return", "fwd_mae", "fwd_vol", "fwd_absmove"):
        day[f"{prefix}_{h}"] = 1.0
    day[f"fwd_tb_{h}"] = 0
    samples = T._emit_samples(day, [h])
    replay_bull = {s["symbol"] for s in samples if s["side"] == "watchlist"}
    replay_bear = {s["symbol"] for s in samples if s["side"] == "caution"}

    assert len(live_bull) >= 8 and len(live_bear) >= 8           # 不是兩個空集合碰巧相等
    assert replay_bull == live_bull and replay_bear == live_bear
    excluded = {r["symbol"] for r in rows if r["amount"] < T.MIN_AMOUNT_TWD or r["sector"] != "電子"}
    assert not (replay_bull | replay_bear) & excluded            # 低流動性與 ETF 一律不進
    assert T._TOP == 8 and T.MIN_AMOUNT_TWD == tw_features.MIN_AMOUNT_TWD
    assert T._ETF_SECTORS == tw_features._ETF_SECTORS


# ── T014 樣本不足／單一類別 → 不訓練、不落模型檔 ──

@pytest.fixture
def train_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(sc, "EDGE_META_PATH", tmp_path / "edge_meta.json")
    monkeypatch.setattr(sc, "_model_path", lambda h: tmp_path / f"edge_model_{h}.pkl")
    monkeypatch.setattr(sc.settings, "enable_edge_model", True)
    monkeypatch.setattr(sc.settings, "edge_model_min_samples", 100)
    monkeypatch.setattr(backtest, "horizons", lambda: [5, 20])
    return tmp_path


def _ds(n, labels):
    ds = pd.DataFrame({c: np.random.default_rng(0).normal(size=n) for c in backtest.FEATURE_COLUMNS})
    ds["__y"], ds["__d"], ds["__w"] = labels, [f"2026-01-{i % 28 + 1:02d}" for i in range(n)], 1.0
    return ds


def test_too_few_samples_skips_training(train_env, monkeypatch):
    monkeypatch.setattr(sc, "_build_dataset", lambda h, *a, **k: _ds(30, [0, 1] * 15))
    meta = sc.train_edge_model()
    assert meta["trained"] is False and "樣本不足 30/100" in meta["reason"]
    assert not list(train_env.glob("edge_model_*.pkl"))
    saved = json.loads((train_env / "edge_meta.json").read_text(encoding="utf-8"))
    assert saved["per_horizon"]["20"]["trained"] is False


def test_single_class_labels_skip_training(train_env, monkeypatch):
    monkeypatch.setattr(sc, "_build_dataset", lambda h, *a, **k: _ds(150, [1] * 150))
    meta = sc.train_edge_model()
    assert meta["trained"] is False and "單一類別" in meta["reason"]


def test_no_dataset_at_all_skips_training(train_env, monkeypatch):
    monkeypatch.setattr(sc, "_build_dataset", lambda h, *a, **k: None)
    assert sc.train_edge_model()["reason"].startswith("樣本不足 0/")


def test_disabled_edge_model_does_not_train(train_env, monkeypatch):
    monkeypatch.setattr(sc.settings, "enable_edge_model", False)
    assert sc.train_edge_model() == {"trained": False, "reason": "disabled"}


# ── T015（spec 019 後為 score_candidates→fuse_scores）無模型 no-op、例外不外洩 ──

CANDS = [{"symbol": "A", "side": "watchlist", "stock_entry": {}}]


def test_edge_without_meta_or_model_is_noop(train_env):
    assert sc.score_candidates(CANDS) == {}


def test_edge_below_auc_gate_is_noop(train_env):
    (train_env / "edge_meta.json").write_text(json.dumps(
        {"per_horizon": {"5": {"holdout_auc": 0.52}, "20": {"holdout_auc": 0.51}}}))
    (train_env / "edge_model_5.pkl").write_bytes(b"x")
    assert sc.score_candidates(CANDS) == {}


def test_edge_scoring_error_is_swallowed(train_env, monkeypatch, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("corrupt model")

    monkeypatch.setattr(sc, "_score_with", boom)
    assert sc.score_candidates(CANDS) == {}
    assert any("edge 打分失敗" in r.getMessage() for r in caplog.records)


# ── T016 precision@k（walk-forward 無洩漏已由 test_walk_forward_leakage 覆蓋）──

def test_precision_at_k_takes_top_fraction_by_probability():
    y = [1, 0] * 50
    p = [0.9 - i * 0.001 if y[i] else 0.1 for i in range(100)]   # 正類機率全部較高
    prec, base = sc._precision_at_k(y, p, frac=0.1)
    assert (prec, base) == (1.0, 0.5)


def test_precision_at_k_needs_enough_samples():
    assert sc._precision_at_k([1, 0] * 9, [0.5] * 18) == (None, None)


def test_precision_at_k_random_scores_track_base_rate():
    rng = np.random.default_rng(0)
    y = (rng.uniform(size=5000) < 0.3).astype(int).tolist()
    prec, base = sc._precision_at_k(y, rng.uniform(size=5000).tolist())
    assert abs(prec - base) < 0.05
