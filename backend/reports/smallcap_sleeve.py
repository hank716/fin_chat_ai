"""小型股候選（spec 024）：由排序模型直接產生的每日名單，與主名單分開呈現、分開追蹤。

為什麼獨立一份名單：主候選池只收日成交額 5,000 萬以上的個股，而目前唯一量得到實質排序力的
模型是小型股帶的 rank 模型（spec 019 訓練了它，卻沒有任何候選會被路由過去）。選股歸因也顯示
主名單的輸贏在候選池就決定了、決策層的挑選沒有顯著加值，所以這份名單**不經決策層增刪重排**——
線上行為要與回測量到的是同一件事：小型股帶內的 5 日漲幅榜 ∪ 外資 5 日買超榜，依模型分數取前幾名。

四道關卡任一不過就不列，並把原因寫進報告（不靜默）：
  功能開關 → 線上成效停損 → 交易限制名單可用 → 模型通過 rank-IC 門檻
"""
from __future__ import annotations

import logging
from typing import Any

from config import settings

logger = logging.getLogger("ai-market-backend.smallcap")

# 線上停損（FR-006）：累積這麼多個到期交易日後，名單的平均超額仍為負就停列。
LIVE_MIN_DAYS = 20
LIQUIDITY_NOTE = "低流動性標的：日成交額僅數千萬元，大單進出會明顯影響價格，部位宜小。"


def _signals(entry: dict[str, Any]) -> list[str]:
    """由資料直接組出的訊號條列（不經模型撰寫，數字必然與 features 一致）。"""
    out: list[str] = []

    def pct(key: str, label: str) -> None:
        v = entry.get(key)
        if isinstance(v, (int, float)):
            out.append(f"{label} {v:+.2f}%")

    pct("return_5d_pct", "5日報酬")
    pct("return_20d_pct", "20日報酬")
    lots = entry.get("foreign_net_buy_5d_lots")
    if isinstance(lots, (int, float)) and lots:
        out.append(f"外資5日{'買超' if lots > 0 else '賣超'}{abs(int(lots)):,}張")
    pct("dist_ma20_pct", "距MA20")
    amount = entry.get("amount_twd")
    if isinstance(amount, (int, float)):
        out.append(f"日成交額 {amount / 1e4:,.0f} 萬元")
    return out


def live_stats(scorecards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """小型股候選到期後的實際成效（與主名單分開統計，FR-005）。逐日平均後再跨日平均。"""
    from reports import backtest

    if scorecards is None:
        scorecards = backtest.load_scorecards(limit=settings.backtest_calibration_lookback)
    h = str(backtest.horizons()[0])
    daily: list[float] = []
    picks = 0
    for card in scorecards:
        rows = [it.get("horizons", {}).get(h) or {} for it in card.get("smallcap_items") or []]
        excess = [r["vs_index_pct"] for r in rows if r.get("vs_index_pct") is not None]
        if excess:
            daily.append(sum(excess) / len(excess))
            picks += len(excess)
    n = len(daily)
    if n == 0:
        return {"n_days": 0, "n_picks": 0}
    return {
        "n_days": n, "n_picks": picks, "horizon": int(h),
        "avg_excess_vs_market_pct": round(sum(daily) / n, 2),
        "beat_market_day_rate": round(sum(1 for v in daily if v > 0) / n, 3),
    }


def live_gate(stats: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """線上成效停損：樣本夠了而平均超額為負 → 停列。樣本不足時放行（還在累積證據）。"""
    stats = live_stats() if stats is None else stats
    if stats.get("n_days", 0) >= LIVE_MIN_DAYS and stats.get("avg_excess_vs_market_pct", 0) < 0:
        return False, (
            f"上線後 {stats['n_days']} 個到期交易日平均輸大盤 "
            f"{abs(stats['avg_excess_vs_market_pct'])}%，依停損條件暫停列出"
        )
    return True, None


def build(tw_feats: dict[str, Any]) -> dict[str, Any]:
    """產生今日小型股候選。回 {"picks": [...], "reason": 未列出的原因或 None, ...}。"""
    from data_sources import trading_restrictions
    from reports import strategy_calibration as sc
    from reports import training_set

    block = tw_feats.get("smallcap") or {}
    out: dict[str, Any] = {"picks": [], "reason": None, "band_twd": block.get("band_twd"),
                           "pool_n": len(block.get("stocks") or {}), "note": LIQUIDITY_NOTE}
    if not settings.enable_smallcap_candidates:
        out["reason"] = "功能未啟用"
        return out
    pool: dict[str, Any] = block.get("stocks") or {}
    if not pool:
        out["reason"] = "今日小型股帶沒有符合條件的候選"
        return out
    ok, why = live_gate()
    if not ok:
        out["reason"] = why
        return out
    restricted = trading_restrictions.restricted_symbols()
    if restricted is None:
        out["reason"] = "處置股／變更交易名單無法取得，為避免列入交易受限個股，今日不列"
        return out
    excluded = sorted(s for s in pool if s in restricted)
    out["excluded_restricted"] = excluded

    try:
        regime = {k: v for k, v in training_set.current_market_regime().items() if v is not None}
    except Exception:  # noqa: BLE001 — 與主名單同紀律：regime 缺值不阻斷打分
        regime = {}
    candidates = [
        {"symbol": sym, "side": "watchlist",
         "stock_entry": {**entry, **regime, "_amount": entry.get("amount_twd")}}
        for sym, entry in pool.items() if sym not in restricted
    ]
    scores = sc._score_rank_band(candidates, "smallcap")
    if not scores:
        out["reason"] = f"小型股排序模型未通過門檻（rank-IC ≥ {settings.rank_ic_gate}），今日不列"
        return out

    top = sorted(candidates, key=lambda c: scores.get(c["symbol"], float("-inf")), reverse=True)
    top = top[: settings.smallcap_top_k]
    risk = sc.score_risk(top)
    for rank, cand in enumerate(top, start=1):
        sym = cand["symbol"]
        entry = pool[sym]
        out["picks"].append({
            "rank": rank, "symbol": sym, "name": entry.get("name") or sym,
            "sector": entry.get("sector"), "close": entry.get("close"),
            "amount_twd": entry.get("amount_twd"),
            "rank_score": scores.get(sym), "risk_score": risk.get(sym),
            "signals": _signals(entry),
        })
    logger.info("小型股候選：池 %d 檔（排除受限 %d）→ 列出 %s",
                len(pool), len(excluded), [p["symbol"] for p in out["picks"]])
    return out
