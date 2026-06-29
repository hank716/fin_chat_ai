"""台股新聞 fact 層（M2-report step D；G 修正新聞新鮮度）。

用 FinMind TaiwanStockNews per-stock 抓近日新聞，正規化成 NewsItem（每則一定有
來源/日期/標題/URL，對齊 design_docs §5.4：AI 不得捏造新聞、所有新聞必須有 source/date/url）。

重要：FinMind TaiwanStockNews 的 start_date 是「單日」語意 —— min==max==start_date，
回的是『那一天』的新聞，不是「該日之後全部」。所以要拿最新新聞必須用最近的交易日當 start_date
（資料日 as_of 與 today），而非往前推一週。
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

import universe
from config import settings

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


def _dedup_key(item: dict[str, Any]) -> str:
    """去重鍵：優先 URL（去 query/scheme），退回正規化標題（去空白/標點）。

    Google 與 FinMind 同一則新聞 URL 常帶不同 query、標題偶有微差 → 兩道防線。
    """
    url = (item.get("url") or "").strip().lower()
    if url:
        url = re.sub(r"^https?://(www\.)?", "", url).split("?")[0].rstrip("/")
        if url:
            return "u:" + url
    title = re.sub(r"[\s\W_]+", "", (item.get("title") or "").lower())
    return "t:" + title


def fetch_news(
    symbols: list[str], *, as_of: str | None = None, per_symbol: int = 2
) -> list[dict[str, Any]]:
    """抓 symbols 最新新聞（資料日 as_of 與 today 兩天，取最新），回 JSON-safe dict list。

    FinMind 為主來源；若開 enable_google_finance_news，並行補上 Google Finance 第二來源並去重
    （FinMind 優先，Google 只補沒見過的）。單檔失敗不阻斷其他。回傳依日期新到舊排序。
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
    merged = [
        {"symbol": n.symbol, "name": n.name, "date": n.date,
         "title": n.title, "source": n.source, "url": n.url, "tier": n.tier,
         "provider": "finmind"}
        for n in out
    ]

    if settings.enable_google_finance_news:
        seen = {_dedup_key(it) for it in merged}
        try:
            from . import google_finance_loader  # 延後匯入避免循環（該模組 top-level 匯入本模組）
            google = google_finance_loader.fetch_news(symbols, per_symbol=per_symbol)
        except Exception as exc:  # noqa: BLE001 — 第二來源整體失敗不可拖垮 FinMind 主來源
            logger.warning("google finance news source failed: %s", exc)
            google = []
        added = 0
        for it in google:
            key = _dedup_key(it)
            if key in seen:
                continue
            seen.add(key)
            merged.append(it)
            added += 1
        logger.info("google finance news: +%d (deduped from %d fetched)", added, len(google))
        merged.sort(key=lambda x: x["date"], reverse=True)

    return merged
