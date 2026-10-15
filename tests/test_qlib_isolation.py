"""spec 015 測試基線：backend 永不 import qlib、serving 只讀 JSON 且受 rank-IC gate 守護、
離線 walk-forward 無未來洩漏且 rank-IC 定義對齊 backend。

spec 019 後 `_apply_qlib_scores` 併入 `fuse_scores`：serving 端的「重排」改由融合分數決定，
這裡照現行路徑驗 `score_qlib` → `fuse_scores`。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from reports import backtest
from reports import strategy_calibration as sc

ROOT = Path(__file__).resolve().parents[1]


# ── T012 靜態檢查：backend 任何檔案都不 import qlib ──

def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_backend_never_imports_qlib():
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "backend").rglob("*.py")
                 if "__pycache__" not in p.parts and "qlib" in _imported_roots(p)]
    assert offenders == [], f"backend 不得 import qlib（隔離在 qlib_offline/ 容器）：{offenders}"


def test_backend_requirements_do_not_pull_pyqlib():
    reqs = (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "pyqlib" not in reqs


# ── serving：score_qlib 受 gate 守護、只讀 JSON ──

CANDS = [{"symbol": s, "side": "watchlist", "stock_entry": {}} for s in ("A", "B", "C")]


@pytest.fixture
def qlib_files(tmp_path, monkeypatch):
    meta, scores = tmp_path / "qlib_meta.json", tmp_path / "qlib_scores"
    monkeypatch.setattr(sc, "QLIB_META_PATH", meta)
    monkeypatch.setattr(sc, "QLIB_SCORES_DIR", scores)
    monkeypatch.setattr(sc.settings, "enable_qlib", True)
    monkeypatch.setattr(sc.settings, "rank_ic_gate", 0.03)

    def write(rank_ic=None, scores_by_day=None):
        if rank_ic is not None:
            meta.write_text(json.dumps({"per_horizon": {str(h): {"rank_ic": rank_ic}
                                                        for h in backtest.horizons()}}))
        for day, sc_ in (scores_by_day or {}).items():
            scores.mkdir(exist_ok=True)
            (scores / f"{day}.json").write_text(json.dumps({"scores": sc_}))

    return write


# T009 無檔 → no-op

def test_no_meta_no_scores_is_noop(qlib_files):
    assert sc.score_qlib(CANDS) == {}


def test_gate_passed_but_no_scores_file_is_noop(qlib_files):
    qlib_files(rank_ic=0.05)
    assert sc.score_qlib(CANDS) == {}


# T010 未過 rank-IC gate → 不打分、不參與融合

def test_below_gate_is_ignored_even_with_scores(qlib_files, monkeypatch):
    qlib_files(rank_ic=0.0145, scores_by_day={"2026-10-14": {"A": 0.1, "B": 0.9, "C": 0.5}})
    assert sc.score_qlib(CANDS) == {}
    monkeypatch.setattr(sc, "score_candidates", lambda c: {})
    monkeypatch.setattr(sc, "score_rank", lambda c: {})
    fused, weights, _ = sc.fuse_scores(CANDS)
    assert fused == {} and weights == {}                      # 呼叫端據此不重排


# T011 過 gate → 只回候選、讀最新一份、融合後依分數排序

def test_passing_gate_scores_candidates_from_latest_file(qlib_files):
    qlib_files(rank_ic=0.05, scores_by_day={
        "2026-10-13": {"A": 0.9, "B": 0.1, "C": 0.5},
        "2026-10-14": {"A": 0.1, "B": 0.9, "C": 0.5, "Z": 1.0},   # Z 不在候選內
    })
    assert sc.score_qlib(CANDS) == {"A": 0.1, "B": 0.9, "C": 0.5}


def test_passing_gate_drives_fused_order(qlib_files, monkeypatch):
    qlib_files(rank_ic=0.05, scores_by_day={"2026-10-14": {"A": 0.1, "B": 0.9, "C": 0.5}})
    monkeypatch.setattr(sc, "score_candidates", lambda c: {})
    monkeypatch.setattr(sc, "score_rank", lambda c: {})
    fused, weights, components = sc.fuse_scores(CANDS)
    assert weights == {"qlib": 1.0}
    assert sorted(fused, key=fused.get, reverse=True) == ["B", "C", "A"]
    assert components["qlib"] == {"A": 0.1, "B": 0.9, "C": 0.5}


def test_disabled_switch_is_noop(qlib_files, monkeypatch):
    qlib_files(rank_ic=0.05, scores_by_day={"2026-10-14": {"A": 0.1}})
    monkeypatch.setattr(sc.settings, "enable_qlib", False)
    assert sc.score_qlib(CANDS) == {}


# ── T013 離線 walk-forward：無未來洩漏；rank-IC 定義對齊 backend ──

class _RecordingLGBM:
    """記下每一折的 train/test 列，predict 回一個能手算 IC 的值。"""

    fits: list[np.ndarray] = []

    def __init__(self, **_k):
        pass

    def fit(self, X, y):
        _RecordingLGBM.fits.append(X[:, 0].copy())      # 第 0 欄放的是日期序號
        return self

    def predict(self, X):
        return X[:, 1]                                   # 第 1 欄＝完美預測（等於目標）


@pytest.fixture
def qlib_run(monkeypatch):
    monkeypatch.setitem(sys.modules, "lightgbm",
                        types.SimpleNamespace(LGBMRegressor=_RecordingLGBM, LGBMClassifier=_RecordingLGBM))
    monkeypatch.syspath_prepend(str(ROOT / "qlib_offline"))
    spec = importlib.util.spec_from_file_location("qlib_run_under_test", ROOT / "qlib_offline" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _RecordingLGBM.fits = []
    return mod


def _panel(n_days=100, n_syms=30, seed=0):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2025-01-01", periods=n_days)
    idx = pd.MultiIndex.from_product([days, [f"S{i}" for i in range(n_syms)]],
                                     names=["datetime", "instrument"])
    df = pd.DataFrame(index=idx)
    df["dpos"] = np.repeat(np.arange(n_days), n_syms).astype(float)
    df["target"] = rng.normal(size=len(df))
    df["signal"] = df["target"]
    return df


def test_walk_forward_trains_only_on_past_with_embargo(qlib_run):
    h = 5
    df = _panel()
    pred, tgt, dpos, folds = qlib_run._walk_forward(df, ["dpos", "signal"], "target", h, classifier=False)
    nd = 100
    edges = [round(nd * k / 5) for k in range(6)]
    assert folds == 4
    for k, train_dpos in enumerate(_RecordingLGBM.fits, start=1):
        assert train_dpos.max() < edges[k] - h               # 訓練資料都早於 test 起點 h 天以上
    assert dpos.min() >= edges[1]                            # 第一段只當訓練，不出 OOS
    np.testing.assert_allclose(pred, tgt)


def test_rank_metrics_is_mean_of_daily_spearman(qlib_run):
    from scipy.stats import spearmanr

    rng = np.random.default_rng(1)
    dpos = np.repeat(np.arange(10), 8)
    tgt = rng.normal(size=dpos.size)
    pred = tgt + rng.normal(scale=1.0, size=dpos.size)
    out = qlib_run._rank_metrics(pred, tgt, dpos)
    daily = [spearmanr(pred[dpos == d], tgt[dpos == d])[0] for d in range(10)]
    assert out["rank_ic"] == pytest.approx(round(float(np.mean(daily)), 4))
    assert out["n_days"] == 10
    perfect = qlib_run._rank_metrics(tgt, tgt, dpos)
    assert perfect["rank_ic"] == 1.0


def test_offline_gate_matches_backend_gate(qlib_run):
    assert qlib_run._RANK_GATE == pytest.approx(sc.settings.rank_ic_gate)
