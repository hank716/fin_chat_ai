"""台股新聞 fact 層（M2-report step D）。

用 FinMind TaiwanStockNews per-stock 抓近日新聞，正規化成 NewsItem（每則一定有
來源/日期/標題/URL，對齊 design_docs §5.4：AI 不得捏造新聞、所有新聞必須有 source/date/url）。

只給聚焦標的（候選/權值/movers）抓，避免 48 檔 × 全量新聞淹沒報告與請求數。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import universe

from . import finmind_loader

logger = logging.getLogger("ai-market-backend.news")


@dataclass
class NewsItem:
    symbol: str
    name: str
    date: str          # YYYY-MM-DD
    title: str
    source: str
    url: str


def _fetch_symbol_news(symbol: str, start_date: str, limit: int) -> list[NewsItem]:
    raw = finmind_loader.get_taiwan_stock_news(symbol, start_date)
    items: list[NewsItem] = []
    for r in raw:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        d = str(r.get("date") or "")[:10]
        items.append(
            NewsItem(
                symbol=symbol,
                name=universe.display_name(symbol),
                date=d,
                title=title,
                source=(r.get("source") or "").strip(),
                url=(r.get("link") or "").strip(),
            )
        )
    # 最新在前，取 limit 則
    items.sort(key=lambda x: x.date, reverse=True)
    return items[:limit]


def fetch_news(
    symbols: list[str], *, days: int = 3, per_symbol: int = 3
) -> list[dict[str, Any]]:
    """抓 symbols 近 days 日新聞（每檔最多 per_symbol 則），回 JSON-safe dict list。

    單檔失敗不阻斷其他。回傳依日期新到舊排序。
    """
    start_date = (date.today() - timedelta(days=days)).isoformat()
    out: list[NewsItem] = []
    for sym in symbols:
        try:
            out.extend(_fetch_symbol_news(sym, start_date, per_symbol))
        except Exception as exc:  # noqa: BLE001 — 單檔新聞失敗不阻斷
            logger.warning("news fetch %s failed: %s", sym, exc)
    out.sort(key=lambda x: x.date, reverse=True)
    return [vars(n) for n in out]
