"""選股歸因：最終名單輸贏大盤，價值是在哪一關流失（或產生）的。

`evaluation.json` 只回答「LLM 選的 5 檔偏多有沒有贏大盤」。這裡把同一天的三層攤開對照：

1. **候選池**（features 裡給 LLM 看的 movers 名單，各自與合計）——獵場本身有沒有超額
2. **LLM 實選**（tw_watchlist / tw_caution）——從池裡挑，比池子平均好還是差
3. **模型分數**（報告當下落地的 risk / meta / rank / fused / size_weights）——當時的分數
   對事後結果有沒有排序力。這些分數是出報告那一刻算的，對之後的行情是真正的樣本外。

全部回溯自已落地的報告 JSON + 本機 parquet，零 LLM 成本、不改 serving 行為。

    docker compose exec -T backend python -m reports.selection_attribution

⚠️ 報酬以除權息還原價計（與 scorecard 同基準）、未扣交易成本；以「逐日平均、再跨日平均」
彙總，避免候選多的那幾天主導結果。t 值只看跨日離散度，交易日少時僅供參考。
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from reports import backtest

BULL_LISTS = ("top_gainers_5d", "top_foreign_buy_5d")
BEAR_LISTS = ("top_losers_5d", "top_foreign_sell_5d", "top_short_margin_ratio", "top_below_index_20d")
SCORE_KEYS = ("risk_scores", "meta_scores", "rank_scores", "fused_scores", "edge_scores", "qlib_scores")


def _outcome(sym: str, as_of: pd.Timestamp, h: int, index_ret: float) -> dict[str, float] | None:
    """單檔 h 日後的超額報酬與最大不利幅度（%）；窗未滿或缺價回 None。"""
    entry = backtest._entry_close_adj(sym, as_of)
    fwd = backtest._forward_window(sym, as_of, h, adjusted=True)
    if not entry or entry <= 0 or len(fwd) < h:
        return None
    ret = (float(fwd["close"].iloc[-1]) - entry) / entry * 100
    mae = (float(fwd["low"].astype(float).min()) - entry) / entry * 100
    return {"ret": ret, "excess": ret - index_ret, "mae": mae}


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rho = pd.Series(xs).corr(pd.Series(ys), method="spearman")
    return None if pd.isna(rho) else float(rho)


def _summary(daily: list[float]) -> dict[str, Any]:
    """逐日數值 → 跨日平均、t 值、勝率（>0 的日數比）。"""
    n = len(daily)
    if n == 0:
        return {"n_days": 0}
    mean = sum(daily) / n
    out: dict[str, Any] = {"n_days": n, "mean": round(mean, 3),
                           "positive_rate": round(sum(1 for v in daily if v > 0) / n, 3)}
    if n >= 2:
        var = sum((v - mean) ** 2 for v in daily) / (n - 1)
        out["t"] = round(mean / math.sqrt(var / n), 2) if var > 0 else None
    return out


def analyze_report(report: dict[str, Any], h: int) -> dict[str, Any] | None:
    """一份報告的各群組當日平均超額 + 當下分數與事後結果的日內相關。未到期回 None。"""
    feats = report.get("features") or {}
    as_of_str = feats.get("as_of") or report.get("data_as_of")
    if not as_of_str:
        return None
    as_of = pd.to_datetime(as_of_str)
    index_ret = backtest._index_forward_return(as_of, h)
    if index_ret is None or not backtest._data_matured(as_of, h):
        return None
    tw = feats.get("tw") or {}
    movers = tw.get("movers") or {}

    groups: dict[str, set[str]] = {
        "llm_watchlist": {w.get("symbol") for w in report.get("tw_watchlist") or []},
        "llm_caution": {w.get("symbol") for w in report.get("tw_caution") or []},
        "pool_all": set((tw.get("stocks") or {}).keys()),
        "pool_bull": {it["symbol"] for k in BULL_LISTS for it in movers.get(k) or []},
        "pool_bear": {it["symbol"] for k in BEAR_LISTS for it in movers.get(k) or []},
    }
    for name, items in movers.items():
        groups["list:" + name] = {it["symbol"] for it in items or []}

    cache: dict[str, dict[str, float] | None] = {}

    def outcome(sym: str) -> dict[str, float] | None:
        if sym not in cache:
            cache[sym] = _outcome(sym, as_of, h, index_ret)
        return cache[sym]

    day: dict[str, Any] = {"as_of": as_of_str, "groups": {}, "scores": {}}
    for name, syms in groups.items():
        rows = [o for o in (outcome(s) for s in syms if s) if o]
        if rows:
            day["groups"][name] = {
                "n": len(rows),
                "excess": sum(r["excess"] for r in rows) / len(rows),
                "mae": sum(r["mae"] for r in rows) / len(rows),
            }

    # 當下落地的模型分數 vs 事後結果（日內 Spearman；分數涵蓋偏多+避雷共 ~10 檔）
    for key in SCORE_KEYS:
        scored = [(float(v), outcome(s)) for s, v in (report.get(key) or {}).items()]
        scored = [(v, o) for v, o in scored if o]
        if len(scored) >= 3:
            xs = [v for v, _ in scored]
            day["scores"][key] = {
                "n": len(scored),
                "ic_excess": _spearman(xs, [o["excess"] for _, o in scored]),
                # 回撤深度取正值（越大越痛），risk 分數有效＝正相關
                "ic_drawdown": _spearman(xs, [-o["mae"] for _, o in scored]),
            }

    weights = report.get("size_weights") or {}
    sized = [(float(w), outcome(s)) for s, w in weights.items()]
    sized = [(w, o) for w, o in sized if o]
    wsum = sum(w for w, _ in sized)
    if sized and wsum > 0:
        day["sizing"] = {
            "weighted_excess": sum(w * o["excess"] for w, o in sized) / wsum,
            "equal_excess": sum(o["excess"] for _, o in sized) / len(sized),
        }
    return day


def aggregate(days: Iterable[dict[str, Any]], h: int) -> dict[str, Any]:
    days = list(days)
    by_group: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_score: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    picks_vs_pool: list[float] = []
    caution_vs_pool: list[float] = []
    sizing_gain: list[float] = []
    for day in days:
        g = day["groups"]
        for name, row in g.items():
            by_group[name]["excess"].append(row["excess"])
            by_group[name]["mae"].append(row["mae"])
            by_group[name]["n"].append(row["n"])
        if "llm_watchlist" in g and "pool_bull" in g:
            picks_vs_pool.append(g["llm_watchlist"]["excess"] - g["pool_bull"]["excess"])
        if "llm_caution" in g and "pool_bear" in g:
            caution_vs_pool.append(g["llm_caution"]["excess"] - g["pool_bear"]["excess"])
        for key, row in day["scores"].items():
            for metric in ("ic_excess", "ic_drawdown"):
                if row.get(metric) is not None:
                    by_score[key][metric].append(row[metric])
        if "sizing" in day:
            sizing_gain.append(day["sizing"]["weighted_excess"] - day["sizing"]["equal_excess"])

    dates = sorted(d["as_of"] for d in days)
    return {
        "horizon": h,
        "n_reports": len(days),
        "date_range": [dates[0], dates[-1]] if dates else [],
        "groups": {
            name: {
                "avg_names": round(sum(v["n"]) / len(v["n"]), 1),
                "excess_pct": _summary(v["excess"]),
                "avg_mae_pct": round(sum(v["mae"]) / len(v["mae"]), 2),
            } for name, v in sorted(by_group.items())
        },
        # LLM 實選 − 同日同向候選池：>0 代表挑選這一關有加值
        "llm_watchlist_minus_pool_bull": _summary(picks_vs_pool),
        "llm_caution_minus_pool_bear": _summary(caution_vs_pool),
        "score_ic": {
            key: {metric: _summary(vals) for metric, vals in v.items()}
            for key, v in sorted(by_score.items())
        },
        "sizing_weighted_minus_equal": _summary(sizing_gain),
    }


def run(reports_dir: str | Path | None = None, hs: list[int] | None = None) -> dict[str, Any]:
    root = Path(reports_dir) if reports_dir is not None else backtest.REPORTS_DIR
    hs = hs or backtest.horizons()
    reports = []
    for path in sorted(root.glob("morning_*.json")):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    out: dict[str, Any] = {}
    for h in hs:
        days = [d for d in (analyze_report(r, h) for r in reports) if d]
        out[str(h)] = aggregate(days, h)
    return out


def _fmt(s: dict[str, Any]) -> str:
    if not s.get("n_days"):
        return "n/a"
    t = s.get("t")
    return "{:+.2f}%  (t={}, 正日比 {:.0%}, {} 日)".format(
        s["mean"], "n/a" if t is None else "{:+.1f}".format(t), s["positive_rate"], s["n_days"])


def _fmt_ic(s: dict[str, Any]) -> str:
    if not s.get("n_days"):
        return "n/a"
    t = s.get("t")
    return "{:+.3f} (t={}, {} 日)".format(
        s["mean"], "n/a" if t is None else "{:+.1f}".format(t), s["n_days"])


def render(result: dict[str, Any]) -> str:
    lines: list[str] = []
    for h, agg in result.items():
        lines.append("=== 選股歸因 h={}（{} 篇，{} ~ {}）===".format(
            h, agg["n_reports"], *(agg["date_range"] or ["-", "-"])))
        lines.append("--- 各群組 h 日超額報酬（vs 大盤，逐日平均後跨日平均）---")
        for name, row in agg["groups"].items():
            lines.append("  {:<28} {:>5.1f} 檔/日  超額 {}  平均最大回撤 {:+.2f}%".format(
                name, row["avg_names"], _fmt(row["excess_pct"]), row["avg_mae_pct"]))
        lines.append("--- 挑選這一關的加值 ---")
        lines.append("  LLM 偏多 − 偏多候選池： " + _fmt(agg["llm_watchlist_minus_pool_bull"]))
        lines.append("  LLM 避雷 − 偏空候選池： " + _fmt(agg["llm_caution_minus_pool_bear"]))
        lines.append("--- 當下落地的模型分數 vs 事後結果（日內 Spearman，跨日平均）---")
        for key, row in agg["score_ic"].items():
            lines.append("  {:<13} 對超額報酬 {}   對回撤深度 {}".format(
                key, _fmt_ic(row.get("ic_excess", {})), _fmt_ic(row.get("ic_drawdown", {}))))
        lines.append("  sizing 加權 − 等權： " + _fmt(agg["sizing_weighted_minus_equal"]))
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    import sys

    result = run(sys.argv[1] if len(sys.argv) > 1 else None)
    out_path = Path(backtest.settings.local_storage_path) / "strategy" / "selection_attribution.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(result))


if __name__ == "__main__":
    main()
