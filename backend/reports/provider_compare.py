"""決策供應商對照比較（spec 026 US4，SC-005）。

    docker compose exec -T backend python -m reports.provider_compare --since 20261022

只比較「同一份主報告與其影子都存在」的配對日：同一天、同一份 features + facts pack，
差別只剩決策模型。輸出並排指標與三條切換門檻——三條都成立才建議切換預設：

1. 單篇成本：OpenAI ≤ Claude 的 60%
2. 裁決率（confirmed + contradicted / 線索數）：OpenAI ≥ Claude 的 80%
3. guardrail errors：OpenAI 不多於 Claude

裁決率直接讀報告裡已由 `verification_stats.classify_outcomes` 分好類的 `clues`，
不在這裡重寫分類邏輯。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from reports import verification_stats as vs

COST_RATIO_MAX = 0.60
ADJUDICATION_RATIO_MIN = 0.80
SHADOW_SUFFIX = "_openai"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _in_window(day: str, since: str | None, until: str | None) -> bool:
    if not day:
        return False
    return not ((since and day < since) or (until and day > until))


def _metrics(report: dict[str, Any], *, retrieval_twd: float | None = None) -> dict[str, Any]:
    """單篇的比較指標。影子傳入主報告的召回成本（召回共用、只付一次）。"""
    cost = report.get("cost") or {}
    verification = cost.get("verification") or report.get("verification") or {}
    clues = verification.get("clues") or []
    adjudicated = sum(1 for c in clues if c.get("outcome") in vs.ADJUDICATED)
    tokens = cost.get("tokens") or {}
    retrieval = float(cost.get("retrieval_twd") or 0.0) if retrieval_twd is None else retrieval_twd
    output = cost.get("decision_output_tokens")
    if output is None:
        output = tokens.get("output_tokens", 0)
    return {
        "cost_twd": float(cost.get("decision_twd") or 0.0) + retrieval,
        "clues": len(clues),
        "adjudicated": adjudicated,
        "guardrail_errors": int((report.get("guardrail") or {}).get("error_count") or 0),
        "output_tokens": int(output or 0),
        "reasoning_tokens": int(tokens.get("reasoning_tokens") or 0),
        "fetch_requests": int(verification.get("fetch_requests") or 0),
        "model": str(cost.get("decision_model") or report.get("decision_model") or ""),
    }


def collect(reports_dir: Path, *, since: str | None = None,
            until: str | None = None) -> dict[str, Any]:
    """配對主報告與影子，回彙總。目錄不存在 raise FileNotFoundError。"""
    if not reports_dir.is_dir():
        raise FileNotFoundError(f"報告目錄不存在：{reports_dir}")
    shadow_dir = reports_dir / "shadow"
    pairs: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    shadow_failed: list[str] = []
    skipped: dict[str, int] = {}
    mains_n = 0
    shadows_n = 0

    for main_path in sorted(reports_dir.glob("morning_*.json")):
        day = vs._report_date(main_path)
        if not _in_window(day, since, until):
            continue
        mains_n += 1
        shadow_path = shadow_dir / f"{main_path.stem}{SHADOW_SUFFIX}.json"
        if not shadow_path.exists():
            skipped["no_shadow"] = skipped.get("no_shadow", 0) + 1
            continue
        shadows_n += 1
        main = _load(main_path)
        shadow = _load(shadow_path)
        if main is None or shadow is None:
            skipped["unreadable"] = skipped.get("unreadable", 0) + 1
            continue
        main_cost = main.get("cost") or {}
        if main_cost.get("frugal_mode") or main_cost.get("decision_provider") != "anthropic":
            # 主報告那天不是 Claude 完整版 → 沒有對照組
            skipped["main_not_comparable"] = skipped.get("main_not_comparable", 0) + 1
            continue
        if shadow.get("error"):
            shadow_failed.append(f"{day}: {shadow['error']}")
            continue
        main_m = _metrics(main)
        shadow_m = _metrics(shadow, retrieval_twd=float(main_cost.get("retrieval_twd") or 0.0))
        pairs.append((day, main_m, shadow_m))

    return _summarize(pairs, shadow_failed, skipped, mains_n=mains_n, shadows_n=shadows_n)


def _side(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    costs = [m["cost_twd"] for m in metrics]
    clues = sum(m["clues"] for m in metrics)
    adjudicated = sum(m["adjudicated"] for m in metrics)
    n = len(metrics) or 1
    models = sorted({m["model"] for m in metrics if m["model"]})
    return {
        "model": ", ".join(models) or "?",
        "cost_mean": round(statistics.fmean(costs), 4) if costs else None,
        "cost_median": round(statistics.median(costs), 4) if costs else None,
        "clues": clues,
        "adjudicated": adjudicated,
        "adjudication_rate": round(adjudicated / clues, 4) if clues else None,
        "guardrail_errors": sum(m["guardrail_errors"] for m in metrics),
        "output_tokens_mean": round(sum(m["output_tokens"] for m in metrics) / n),
        "reasoning_tokens_mean": round(sum(m["reasoning_tokens"] for m in metrics) / n),
        "fetch_requests_mean": round(sum(m["fetch_requests"] for m in metrics) / n, 2),
    }


def _summarize(pairs, shadow_failed, skipped, *, mains_n: int, shadows_n: int) -> dict[str, Any]:
    main = _side([p[1] for p in pairs])
    shadow = _side([p[2] for p in pairs])
    gates: dict[str, Any] = {}
    if pairs:
        cost_ratio = (shadow["cost_mean"] / main["cost_mean"]) if main["cost_mean"] else None
        if main["adjudication_rate"]:
            adj_ratio = (shadow["adjudication_rate"] or 0.0) / main["adjudication_rate"]
        else:
            # Claude 那邊裁決率為 0 或沒有線索：只要 OpenAI 不是更差（也 0）就算過——
            # 分母 0 時無從比例，不能讓這條門檻因除以零而永遠不成立或永遠成立
            adj_ratio = None
        gates = {
            "cost": {"ratio": None if cost_ratio is None else round(cost_ratio, 4),
                     "pass": cost_ratio is not None and cost_ratio <= COST_RATIO_MAX},
            "adjudication": {
                "ratio": None if adj_ratio is None else round(adj_ratio, 4),
                "pass": (adj_ratio >= ADJUDICATION_RATIO_MIN) if adj_ratio is not None
                else (shadow["adjudication_rate"] or 0.0) >= (main["adjudication_rate"] or 0.0),
            },
            "guardrail": {"pass": shadow["guardrail_errors"] <= main["guardrail_errors"]},
        }
        gates["recommend_switch"] = all(g["pass"] for g in gates.values())
    days = [p[0] for p in pairs]
    return {
        "pairs": len(pairs),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "mains_in_window": mains_n,
        "shadows_in_window": shadows_n,
        "shadow_failed": len(shadow_failed),
        "shadow_failures": shadow_failed,
        "skipped": skipped,
        "main": main,
        "shadow": shadow,
        "gates": gates,
    }


def _d(day: str | None) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:]}" if day and len(day) == 8 else "?"


def _num(v: Any, fmt: str = "{:,.0f}") -> str:
    return "—" if v is None else fmt.format(v)


def _pct(v: float | None) -> str:
    return "n/a" if v is None else "{:.0f}%".format(v * 100)


def render(summary: dict[str, Any]) -> str:
    m, s = summary["main"], summary["shadow"]
    lines = [
        "配對天數：{}（{} ~ {}），影子失敗 {} 次".format(
            summary["pairs"], _d(summary["first_day"]), _d(summary["last_day"]),
            summary["shadow_failed"]),
        "",
        "{:<26}{:>20}{:>20}".format("", m["model"][:18], s["model"][:18]),
        "{:<22}{:>20}{:>20}".format(
            "單篇成本 平均/中位 NT$",
            "{} / {}".format(_num(m["cost_mean"], "{:.1f}"), _num(m["cost_median"], "{:.1f}")),
            "{} / {}".format(_num(s["cost_mean"], "{:.1f}"), _num(s["cost_median"], "{:.1f}"))),
        "{:<24}{:>20}{:>20}".format(
            "裁決率", _num(m["adjudication_rate"], "{:.2f}"),
            _num(s["adjudication_rate"], "{:.2f}")),
        "{:<20}{:>20}{:>20}".format(
            "guardrail errors（合計）", m["guardrail_errors"], s["guardrail_errors"]),
        "{:<22}{:>20}{:>20}".format(
            "output tokens 平均", _num(m["output_tokens_mean"]), _num(s["output_tokens_mean"])),
        "{:<22}{:>20}{:>20}".format(
            "  其中 reasoning 平均", "—", _num(s["reasoning_tokens_mean"])),
        "{:<24}{:>20}{:>20}".format(
            "查證次數 平均", _num(m["fetch_requests_mean"], "{:.1f}"),
            _num(s["fetch_requests_mean"], "{:.1f}")),
    ]
    gates = summary["gates"]
    if gates:
        mark = (lambda ok: "✅" if ok else "❌")
        lines += [
            "",
            "切換門檻（SC-005）",
            "  成本 ≤ 60%        {} {}".format(mark(gates["cost"]["pass"]),
                                             _pct(gates["cost"]["ratio"])),
            "  裁決率 ≥ 80%      {} {}".format(mark(gates["adjudication"]["pass"]),
                                             _pct(gates["adjudication"]["ratio"])),
            "  違規不多於 Claude  {}".format(mark(gates["guardrail"]["pass"])),
            "→ 三項成立：建議切換預設" if gates["recommend_switch"]
            else "→ 未全數成立：維持現行預設",
        ]
    if summary["shadow_failures"]:
        lines += ["", "影子失敗："] + [f"  {f}" for f in summary["shadow_failures"]]
    if summary["skipped"]:
        lines += ["", "排除：{}（no_shadow=當天沒跑影子、main_not_comparable=主報告為節儉或降級）"
                  .format(summary["skipped"])]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m reports.provider_compare",
        description="同日配對比較主決策供應商與影子（spec 026 US4）")
    parser.add_argument("reports_dir", nargs="?", default=None,
                        help="報告目錄（預設 LOCAL_STORAGE_PATH/reports；影子在其 shadow/ 子目錄）")
    parser.add_argument("--since", help="只計入此日（含）之後，格式 YYYYMMDD")
    parser.add_argument("--until", help="只計入此日（含）之前，格式 YYYYMMDD")
    parser.add_argument("--json", action="store_true", help="輸出機器可讀 JSON")
    args = parser.parse_args(argv)
    for flag in ("since", "until"):
        value = getattr(args, flag)
        if value and not (len(value) == 8 and value.isdigit()):
            parser.error(f"--{flag} 需為 YYYYMMDD，收到 {value!r}")
    root = Path(args.reports_dir) if args.reports_dir else vs.default_reports_dir()
    try:
        summary = collect(root, since=args.since, until=args.until)
    except FileNotFoundError as exc:
        parser.error(str(exc))
    if summary["mains_in_window"] == 0 or summary["shadows_in_window"] == 0:
        # 不印空表（spec 023 verification_stats 的教訓：空表看起來像「沒有問題」）
        print(f"沒有可比較的報告：主報告 {summary['mains_in_window']} 篇、"
              f"影子 {summary['shadows_in_window']} 篇（目錄 {root}）", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
