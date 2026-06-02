"""台股新聞 fact 層（M2-report step D；G 修正新聞新鮮度）。

用 FinMind TaiwanStockNews per-stock 抓近日新聞，正規化成 NewsItem（每則一定有
來源/日期/標題/URL，對齊 design_docs §5.4：AI 不得捏造新聞、所有新聞必須有 source/date/url）。

重要：FinMind TaiwanStockNews 的 start_date 是「單日」語意 —— min==max==start_date，
回的是『那一天』的新聞，不是「該日之後全部」。所以要拿最新新聞必須用最近的交易日當 start_date
（資料日 as_of 與 today），而非往前推一週。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import universe

from . import finmind_loader

logger = logging.getLogger("ai-market-backend.news")

# 社群/論壇來源 → 只能當情緒訊號（design_docs §3：PTT/Dcard 強制標 inference 不可當事實）。
# 其餘視為權威媒體（authoritative），可當 fact 引用。比對 source 或 title 子字串。
_SOCIAL_MARKERS = (
    "PTT", "ptt", "Dcard", "dcard", "爆料同學會", "Mobile01", "mobile01",
    "巴哈", "論壇", "網友", "Reddit", "reddit", "推特", "X平台",
)


def classify_tier(source: str, title: str) -> str:
    """authoritative（權威媒體，可當事實）/ social（社群論壇，只當情緒訊號）。"""
    blob = f"{source} {title}"
    return "social" if any(m in blob for m in _SOCIAL_MARKERS) else "authoritative"


class NewsItem:
    __slots__ = ("symbol", "name", "date", "title", "source", "url", "tier")

    def __init__(self, symbol, name, date, title, source, url, tier):  # noqa: A002
        self.symbol, self.name, self.date = symbol, name, date
        self.title, self.source, self.url, self.tier = title, source, url, tier


def _fetch_symbol_news(symbol: str, days: list[str], per_symbol: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    seen: set[str] = set()
    for d in days:  # days 已由新到舊排序
        raw = finmind_loader.get_taiwan_stock_news(symbol, d)
        for r in raw:
            title = (r.get("title") or "").strip()
            url = (r.get("link") or "").strip()
            key = url or title
            if not title or key in seen:
                continue
            seen.add(key)
            source = (r.get("source") or "").strip()
            items.append(
                NewsItem(
                    symbol=symbol,
                    name=universe.display_name(symbol),
                    date=str(r.get("date") or "")[:10],
                    title=title,
                    source=source,
                    url=url,
                    tier=classify_tier(source, title),
                )
            )
    items.sort(key=lambda x: x.date, reverse=True)
    return items[:per_symbol]


def fetch_news(
    symbols: list[str], *, as_of: str | None = None, per_symbol: int = 2
) -> list[dict[str, Any]]:
    """抓 symbols 最新新聞（資料日 as_of 與 today 兩天，取最新），回 JSON-safe dict list。

    單檔失敗不阻斷其他。回傳依日期新到舊排序。
    """
    today = date.today().isoformat()
    days = sorted({d for d in (as_of, today) if d}, reverse=True)
    out: list[NewsItem] = []
    for sym in symbols:
        try:
            out.extend(_fetch_symbol_news(sym, days, per_symbol))
        except Exception as exc:  # noqa: BLE001 — 單檔新聞失敗不阻斷
            logger.warning("news fetch %s failed: %s", sym, exc)
    out.sort(key=lambda x: x.date, reverse=True)
    return [
        {"symbol": n.symbol, "name": n.name, "date": n.date,
         "title": n.title, "source": n.source, "url": n.url, "tier": n.tier}
        for n in out
    ]
