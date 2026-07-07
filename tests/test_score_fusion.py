"""WP2.2 方向分數融合測試（spec 019）。

fuse_scores：通過各自 gate 的方向模型(edge/rank/qlib)分數 z-score 標準化後、以超 gate 幅度加權
平均成單一排序分數（取代 last-writer-wins）。monkeypatch 各 score_* 與 margin，純確定性。
"""
from __future__ import annotations

from reports import strategy_calibration as sc


def _cands(syms):
    return [{"symbol": s, "stock_entry": {}, "side": "watchlist"} for s in syms]


def test_fuse_only_one_model_equals_that_ranking(monkeypatch):
    """僅 rank 過 gate → 融合排序＝rank 排序、權重全歸 rank。"""
    monkeypatch.setattr(sc, "score_candidates", lambda c: {})              # edge 未過
    monkeypatch.setattr(sc, "score_rank", lambda c: {"A": 0.1, "B": 0.3, "C": 0.2})
    monkeypatch.setattr(sc, "score_qlib", lambda c: {})                    # qlib 未過
    monkeypatch.setattr(sc, "_meta_margin",
                        lambda mp, g, k: 0.005 if k == "rank_ic" else 0.0)
    fused, weights, comps = sc.fuse_scores(_cands(["A", "B", "C"]))
    assert weights == {"rank": 1.0}
    assert sorted(fused, key=lambda s: fused[s], reverse=True) == ["B", "C", "A"]
    assert comps["rank"] == {"A": 0.1, "B": 0.3, "C": 0.2}
    assert comps["edge"] == {} and comps["qlib"] == {}


def test_fuse_two_models_weighted_by_margin(monkeypatch):
    """edge+rank 皆過 → 權重∝超 gate 幅度（edge margin 大 → edge 主導），和=1。"""
    monkeypatch.setattr(sc, "score_candidates", lambda c: {"A": 0.6, "B": 0.5, "C": 0.55})
    monkeypatch.setattr(sc, "score_rank", lambda c: {"A": 0.1, "B": 0.3, "C": 0.2})
    monkeypatch.setattr(sc, "score_qlib", lambda c: {})
    monkeypatch.setattr(sc, "_meta_margin",
                        lambda mp, g, k: 0.03 if k == "holdout_auc" else 0.01)
    fused, weights, comps = sc.fuse_scores(_cands(["A", "B", "C"]))
    assert set(weights) == {"edge", "rank"}
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert weights["edge"] > weights["rank"]                # edge 幅度較大
    # edge 主導（A 高、B 低）→ A>C>B
    assert sorted(fused, key=lambda s: fused[s], reverse=True) == ["A", "C", "B"]


def test_fuse_none_pass_gate_no_rerank(monkeypatch):
    """全不過 gate → 回空（呼叫端不重排）。"""
    monkeypatch.setattr(sc, "score_candidates", lambda c: {})
    monkeypatch.setattr(sc, "score_rank", lambda c: {})
    monkeypatch.setattr(sc, "score_qlib", lambda c: {})
    monkeypatch.setattr(sc, "_meta_margin", lambda *a: 0.0)
    fused, weights, comps = sc.fuse_scores(_cands(["A", "B"]))
    assert fused == {} and weights == {}


def test_score_rank_routes_by_amount(monkeypatch):
    """WP2.3：候選 amount<50M 走 smallcap 帶、其餘（含缺 _amount）走主池。"""
    seen: dict = {}

    def fake_band(cands, band):
        seen[band] = [c["symbol"] for c in cands]
        return {c["symbol"]: 1.0 for c in cands}

    monkeypatch.setattr(sc.settings, "enable_rank_model", True)
    monkeypatch.setattr(sc, "_score_rank_band", fake_band)
    cands = [
        {"symbol": "BIG", "stock_entry": {"_amount": 100e6}},   # >=50M → 主池
        {"symbol": "SML", "stock_entry": {"_amount": 10e6}},    # <50M → smallcap
        {"symbol": "NOA", "stock_entry": {}},                   # 缺 amount → 主池（安全預設）
    ]
    out = sc.score_rank(cands)
    assert seen[None] == ["BIG", "NOA"]
    assert seen["smallcap"] == ["SML"]
    assert set(out) == {"BIG", "SML", "NOA"}


def test_rank_model_path_band():
    """band 模型檔名帶 band 後綴，主池不變。"""
    assert sc._rank_model_path(5).name == "rank_model_5.pkl"
    assert sc._rank_model_path(5, "smallcap").name == "rank_model_smallcap_5.pkl"
    assert sc._rank_meta_path(None).name == "rank_model_meta.json"
    assert sc._rank_meta_path("smallcap").name == "rank_model_meta_smallcap.json"


def test_fuse_scores_but_zero_margin_excluded(monkeypatch):
    """有分數但 margin=0（防禦性）→ 不納入融合。"""
    monkeypatch.setattr(sc, "score_candidates", lambda c: {"A": 0.6, "B": 0.5})
    monkeypatch.setattr(sc, "score_rank", lambda c: {})
    monkeypatch.setattr(sc, "score_qlib", lambda c: {})
    monkeypatch.setattr(sc, "_meta_margin", lambda *a: 0.0)   # edge margin 0 → 排除
    fused, weights, comps = sc.fuse_scores(_cands(["A", "B"]))
    assert fused == {} and weights == {}
    assert comps["edge"] == {"A": 0.6, "B": 0.5}             # component 仍保留供 report
