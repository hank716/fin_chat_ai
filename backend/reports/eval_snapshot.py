"""WP0.2 — 可重現的評估快照：實跑訓練 pipeline，把**真實** OOS 指標落地供 A/B 對照。

現況痛點：所有成績數字（rank-IC≈0.027 / risk AUC≈0.69 / 小型股 IC≈0.06）都只存在程式
註解裡，storage/strategy/ 在此 checkout 從未被實跑填過。本模組把「跑一次拿真數字」制度化。

CLI：
  # 跑一次完整 baseline（固定資料截止日讓之後可歸因），落地快照
  python -m reports.eval_snapshot --tag baseline --max-date 2024-05-31
  # 對照兩份快照，逐指標印 delta（D1 歸因：同 max_date、只差一項改動）
  python -m reports.eval_snapshot --compare storage/strategy/eval_history/A.json B.json

D1 歸因方法論：任何準確度改動，改動前後在**同一 max_date 資料快照**上各跑一次，逐指標
對照 delta。純本機 parquet + CPU，零外部 API、零 LLM 成本。
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from reports import backtest, strategy_calibration as sc
from reports import training_set

EVAL_HISTORY_DIR = sc.STRATEGY_DIR / "eval_history"


def _git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — 無 git 不阻斷
        return None


def _per_horizon(meta: dict[str, Any], keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """從 train_* 回傳的 meta 抽每個 horizon 的關鍵指標（缺值留 None，不 trained 帶 reason）。"""
    out: dict[str, dict[str, Any]] = {}
    for h, row in (meta.get("per_horizon") or {}).items():
        if not row.get("trained"):
            out[h] = {"trained": False, "reason": row.get("reason"), "n_samples": row.get("n_samples")}
            continue
        out[h] = {"trained": True, "n_samples": row.get("n_samples"), **{k: row.get(k) for k in keys}}
    return out


def run_snapshot(tag: str, *, max_date: str | None = None, smallcap: bool = True) -> dict[str, Any]:
    """實跑 build → train(edge/risk/rank/meta) → sleeve → effectiveness，組快照並落地。"""
    ts_stats = training_set.build_training_set(max_date=max_date)
    if not ts_stats.get("built"):
        return {"ok": False, "reason": ts_stats.get("reason"), "training_set": ts_stats}

    edge = sc.train_edge_model()
    risk = sc.train_risk_model()
    rank = sc.train_rank_model()
    meta = sc.train_meta_model()

    # 小型股/戰場 IC：run_battlefield_experiment 逐池（main/smallcap/event）回 rank_ic，
    # 正是驗收要對照的「小型股 IC≈0.06」來源；另跑 sleeve 淨報酬回測（扣成本後還剩不剩）。
    battlefield: dict[str, Any] | None = None
    sleeve: dict[str, Any] | None = None
    if smallcap:
        try:
            battlefield = sc.run_battlefield_experiment()
        except Exception as exc:  # noqa: BLE001 — 失敗不阻斷快照
            battlefield = {"ran": False, "reason": f"exception: {exc}"}
        try:
            sleeve = sc.backtest_smallcap_sleeve()
        except Exception as exc:  # noqa: BLE001
            sleeve = {"ran": False, "reason": f"exception: {exc}"}

    try:
        effectiveness = sc.evaluate_effectiveness()
    except Exception as exc:  # noqa: BLE001
        effectiveness = {"error": str(exc)}

    snapshot = {
        "tag": tag,
        "generated_at": datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "max_date": max_date,
        "horizons": backtest.horizons(),
        "training_set": {
            "rows": ts_stats.get("rows"),
            "distinct_days": ts_stats.get("distinct_days"),
            "symbols_scanned": ts_stats.get("symbols_scanned"),
            "symbols_used": ts_stats.get("symbols_used"),
            "date_range": ts_stats.get("date_range"),
            "samples_per_horizon": ts_stats.get("samples_per_horizon"),
        },
        "config_gates": {
            "edge_min_auc": sc.EDGE_MIN_AUC,
            "risk_model_min_auc": settings.risk_model_min_auc,
            "rank_ic_gate": settings.rank_ic_gate,
            "meta_model_min_auc": settings.meta_model_min_auc,
            "edge_model_min_samples": settings.edge_model_min_samples,
        },
        "models": {
            "edge": _per_horizon(edge, ("holdout_auc", "holdout_accuracy", "precision_at_k",
                                        "base_rate", "eval_method", "cv_folds")),
            "risk": _per_horizon(risk, ("holdout_auc", "holdout_accuracy", "eval_method", "cv_folds")),
            "rank": _per_horizon(rank, ("rank_ic", "icir", "pooled_ic", "eval_method", "cv_folds")),
            "meta": _per_horizon(meta, ("holdout_auc", "precision_at_k", "base_rate",
                                        "eval_method", "cv_folds")),
        },
        "battlefield": battlefield,
        "smallcap_sleeve": sleeve,
        "effectiveness_verdict": (effectiveness or {}).get("verdict"),
    }

    EVAL_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo(settings.tz)).strftime("%Y%m%d_%H%M%S")
    out_path = EVAL_HISTORY_DIR / f"{stamp}_{tag}.json"
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot["_path"] = str(out_path)
    return {"ok": True, "snapshot": snapshot, "path": str(out_path)}


# ── 對照 ──

def _flatten(snap: dict[str, Any]) -> dict[str, float]:
    """把快照攤平成 {指標鍵: 數值}，供逐指標 delta。只收有數值的主指標。"""
    flat: dict[str, float] = {}
    models = snap.get("models", {})
    metric_of = {"edge": "holdout_auc", "risk": "holdout_auc", "rank": "rank_ic", "meta": "holdout_auc"}
    for model, primary in metric_of.items():
        for h, row in (models.get(model) or {}).items():
            v = row.get(primary)
            if isinstance(v, (int, float)):
                flat[f"{model}_h{h}_{primary}"] = float(v)
            if model == "rank" and isinstance(row.get("icir"), (int, float)):
                flat[f"{model}_h{h}_icir"] = float(row["icir"])
            if model == "meta" and isinstance(row.get("precision_at_k"), (int, float)):
                flat[f"{model}_h{h}_precision_at_k"] = float(row["precision_at_k"])
    # 戰場實驗：main/smallcap/event 各 horizon 的 rank_ic（小型股 IC≈0.06 的來源）
    bf = (snap.get("battlefield") or {}).get("results") or {}
    for pool_h, row in bf.items():
        if isinstance((row or {}).get("rank_ic"), (int, float)):
            flat[f"bf_{pool_h}_rank_ic"] = float(row["rank_ic"])
    # sleeve 淨報酬：選股 alpha 與 hit rate
    sleeve = snap.get("smallcap_sleeve") or {}
    for k in ("selection_alpha_5d_pct", "hit_rate_top_gt_pool"):
        if isinstance(sleeve.get(k), (int, float)):
            flat[f"sleeve_{k}"] = float(sleeve[k])
    rows = (snap.get("training_set") or {}).get("rows")
    if isinstance(rows, (int, float)):
        flat["training_rows"] = float(rows)
    return flat


def compare(path_a: str, path_b: str) -> str:
    a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    b = json.loads(Path(path_b).read_text(encoding="utf-8"))
    fa, fb = _flatten(a), _flatten(b)
    keys = sorted(set(fa) | set(fb))
    lines = [
        f"A = {a.get('tag')} ({a.get('git_sha')}, max_date={a.get('max_date')})",
        f"B = {b.get('tag')} ({b.get('git_sha')}, max_date={b.get('max_date')})",
        "",
        f"{'metric':<32} {'A':>12} {'B':>12} {'delta':>12}",
        "-" * 72,
    ]
    for k in keys:
        va, vb = fa.get(k), fb.get(k)
        d = (vb - va) if (va is not None and vb is not None) else None
        lines.append(f"{k:<32} {_fmt(va):>12} {_fmt(vb):>12} {_fmt(d, plus=True):>12}")
    if a.get("max_date") != b.get("max_date"):
        lines += ["", "⚠️  兩份快照 max_date 不同 → delta 不可歸因（違反 D1，應在同一資料截止日對照）"]
    return "\n".join(lines)


def _fmt(v: float | None, *, plus: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.4f}" if plus else f"{v:.4f}"


def main() -> None:
    ap = argparse.ArgumentParser(description="評估快照：實跑訓練 pipeline 拿真實 OOS 指標")
    ap.add_argument("--tag", help="快照標籤（如 baseline / adj_prices）")
    ap.add_argument("--max-date", help="資料截止日 YYYY-MM-DD（固定資料快照，供 A/B 歸因）")
    ap.add_argument("--no-smallcap", action="store_true", help="略過小型股 sleeve 回測（較快）")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="對照兩份快照 JSON")
    args = ap.parse_args()

    if args.compare:
        print(compare(*args.compare))
        return
    if not args.tag:
        ap.error("需給 --tag 或 --compare")
    res = run_snapshot(args.tag, max_date=args.max_date, smallcap=not args.no_smallcap)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
