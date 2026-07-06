"""eval_snapshot 純函式測試（不實跑 pipeline）：攤平與對照邏輯。"""
from __future__ import annotations

from reports import eval_snapshot as es


def _snap(tag, *, edge_h5, rank_h5, sc_h5, rows, max_date="2026-06-02"):
    return {
        "tag": tag, "git_sha": "abc1234", "max_date": max_date,
        "training_set": {"rows": rows},
        "models": {
            "edge": {"5": {"trained": True, "holdout_auc": edge_h5}},
            "risk": {"5": {"trained": False, "reason": "樣本不足"}},
            "rank": {"5": {"trained": True, "rank_ic": rank_h5, "icir": 0.15}},
            "meta": {"5": {"trained": True, "holdout_auc": 0.6, "precision_at_k": 0.4}},
        },
        "battlefield": {"results": {"smallcap_5": {"rank_ic": sc_h5, "n": 100}}},
        "smallcap_sleeve": {"selection_alpha_5d_pct": 0.8, "hit_rate_top_gt_pool": 0.6},
    }


def test_flatten_picks_primary_metrics():
    flat = es._flatten(_snap("baseline", edge_h5=0.50, rank_h5=0.04, sc_h5=0.055, rows=3943))
    assert flat["edge_h5_holdout_auc"] == 0.50
    assert flat["rank_h5_rank_ic"] == 0.04
    assert flat["rank_h5_icir"] == 0.15
    assert flat["meta_h5_precision_at_k"] == 0.4
    assert flat["bf_smallcap_5_rank_ic"] == 0.055
    assert flat["sleeve_selection_alpha_5d_pct"] == 0.8
    assert flat["training_rows"] == 3943
    # 未 trained 的 risk 不應出現在攤平結果（沒有數值主指標）
    assert not any(k.startswith("risk_") for k in flat)


def test_compare_reports_deltas():
    a = _snap("baseline", edge_h5=0.50, rank_h5=0.030, sc_h5=0.055, rows=3900)
    b = _snap("adj_prices", edge_h5=0.53, rank_h5=0.045, sc_h5=0.061, rows=3950)
    import json
    import tempfile
    import pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "a.json").write_text(json.dumps(a))
    (d / "b.json").write_text(json.dumps(b))
    out = es.compare(str(d / "a.json"), str(d / "b.json"))
    assert "rank_h5_rank_ic" in out
    assert "+0.0150" in out            # 0.045 - 0.030
    assert "+0.0300" in out            # edge 0.53 - 0.50


def test_compare_warns_on_mismatched_max_date():
    a = _snap("baseline", edge_h5=0.5, rank_h5=0.03, sc_h5=0.05, rows=100, max_date="2026-06-02")
    b = _snap("other", edge_h5=0.5, rank_h5=0.03, sc_h5=0.05, rows=100, max_date="2026-05-01")
    import json
    import pathlib
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "a.json").write_text(json.dumps(a))
    (d / "b.json").write_text(json.dumps(b))
    out = es.compare(str(d / "a.json"), str(d / "b.json"))
    assert "max_date 不同" in out       # 違反 D1 時要警告
