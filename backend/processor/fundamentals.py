"""基本面（M8-4）：月營收 YoY/MoM（on-demand，對齊 design §5.2）。

全市場 2000+ 檔不可能每天全抓財報，故對「焦點標的」（movers/watchlist/問答標的）
on-demand 抓 FinMind 月營收並算 YoY/MoM。process 內 lru 快取（月營收月更，當日重複查不重抓）。
EPS/季報留後續。
"""
from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache
from typing import Any

logger = logging.getLogger("ai-market-backend.fundamentals")


@lru_cache(maxsize=2048)
def build_fundamentals(symbol: str) -> dict[str, Any] | None:
    """單檔月營收 + YoY/MoM（億元）。查無回 None。"""
    from data_sources import finmind_loader

    try:
        start = (date.today().replace(year=date.today().year - 2)).isoformat()
        rows = finmind_loader.get_month_revenue(symbol, start)
    except Exception as exc:  # noqa: BLE001
        logger.warning("月營收抓取 %s 失敗: %s", symbol, exc)
        return None
    if not rows:
        return None

    # 依 (year, month) 排序
    rows = sorted(rows, key=lambda r: (r.get("revenue_year", 0), r.get("revenue_month", 0)))
    latest = rows[-1]
    rev = latest.get("revenue")
    if rev in (None, ""):
        return None
    y, m = latest.get("revenue_year"), latest.get("revenue_month")

    def _find(yy: int, mm: int) -> float | None:
        for r in rows:
            if r.get("revenue_year") == yy and r.get("revenue_month") == mm:
                v = r.get("revenue")
                return float(v) if v not in (None, "") else None
        return None

    rev = float(rev)
    prev_m = _find(y, m - 1) if m and m > 1 else _find((y or 0) - 1, 12)
    yoy_base = _find((y or 0) - 1, m)
    mom = round((rev / prev_m - 1) * 100, 1) if prev_m else None
    yoy = round((rev / yoy_base - 1) * 100, 1) if yoy_base else None
    return {
        "revenue_month": f"{y}-{m:02d}" if y and m else None,
        "revenue_100m": round(rev / 1e8, 1),   # 億元
        "revenue_yoy_pct": yoy,
        "revenue_mom_pct": mom,
    }
