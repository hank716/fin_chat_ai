"""Google Finance 個股頁新聞（FinMind 之外的第二來源並行；不取代）。

Google Finance 無官方 API（2012 起停掉），唯一路徑是爬個股頁 `In the news` 區塊——
所幸該區塊**伺服器端渲染在初始 HTML**（不需執行 JS），每則含來源/相對時間/標題/外連結。

⚠️ 風險與緩解（誠實面對）：
  1. 違反 Google ToS、有 IP 封鎖/captcha 風險 → 過 rate_limiter('google_finance')、保守速率 + 瀏覽器 UA，
     429/403 視為被擋直接回空、不重試（單檔失敗不阻斷，FinMind 仍是主來源）。
  2. class name 混淆且會無預警改版 → 解析靜默失效時記 log，呼叫端可監測「某檔回 0 則」。
  3. 時間是相對字串（「2 小時前」/「2 days ago」）→ 轉絕對日期僅供新鮮度排序，舊新聞精度低可接受。

涵蓋面：大型/熱門股（多為上市 TPE）新聞豐富；冷門上櫃小型股 Google 多半無新聞 → 正常回空。
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

import httpx
import universe
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from data_sources import rate_limiter
from data_sources.news_loader import NewsItem, classify_tier

logger = logging.getLogger("ai-market-backend.google_finance")

_QUOTE_URL = "https://www.google.com/finance/quote/{ticker}"
_TIMEOUT = 20.0
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# market_of() 回 "twse"/"tpex" → Google Finance 交易所後綴。
_EXCHANGE = {"twse": "TPE", "tpex": "TWO"}

# 結構錨點：來源(WrUjhf) → 相對時間(JQ8Czd) → 外連結 anchor(target=_blank) + 標題 div。
# 非貪婪 .*? 讓每則 source/time/anchor 依序配對。class 名會改版 → 改版時整體回 0、由呼叫端監測。
_ITEM_RE = re.compile(
    r'class="WrUjhf">(?P<source>.*?)</div>'
    r'.*?class="JQ8Czd">(?P<time>.*?)</div>'
    r'.*?<a href="(?P<url>https?://[^"]+)"[^>]*target="_blank"[^>]*>\s*'
    r'<div[^>]*>(?P<title>.*?)</div>',
    re.S,
)

_TAG_RE = re.compile(r"<[^>]+>")
# 相對時間單位 → 天數權重（週/月用近似；只為新鮮度排序，不需精準）。
_REL_UNITS = (
    (("分鐘", "分", "minute", "min"), 0),
    (("小時", "時", "hour", "hr"), 0),
    (("天", "日", "day"), 1),
    (("週", "周", "星期", "week"), 7),
    (("個月", "月", "month"), 30),
    (("年", "year"), 365),
)


def _google_ticker(symbol: str) -> str | None:
    """台股代號 → Google Finance ticker（如 2330:TPE / 6488:TWO）。非台股回 None（不支援）。"""
    exch = _EXCHANGE.get(universe.market_of(symbol) or "")
    return f"{symbol}:{exch}" if exch else None


def _clean(text: str) -> str:
    """去 HTML tag + 解 entity + 收斂空白。"""
    t = _TAG_RE.sub("", text)
    t = (t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&#39;", "'").replace("&quot;", '"').replace("\xa0", " "))
    return re.sub(r"\s+", " ", t).strip()


def _relative_to_date(text: str, today: date) -> str:
    """「19 小時前」「2 天前」「3 days ago」「1 個月前」→ 絕對 ISO 日期。無法解析回 today。"""
    blob = _clean(text)
    m = re.search(r"(\d+)", blob)
    n = int(m.group(1)) if m else 0
    for kws, per in _REL_UNITS:
        if any(k in blob for k in kws):
            return (today - timedelta(days=n * per)).isoformat()
    return today.isoformat()


def _parse_news(html: str, symbol: str, today: date) -> list[NewsItem]:
    items: list[NewsItem] = []
    seen: set[str] = set()
    for m in _ITEM_RE.finditer(html):
        title = _clean(m.group("title"))
        url = m.group("url").strip()
        if not title or url in seen:
            continue
        seen.add(url)
        source = _clean(m.group("source"))
        items.append(
            NewsItem(
                symbol=symbol,
                name=universe.display_name(symbol),
                date=_relative_to_date(m.group("time"), today),
                title=title,
                source=source,
                url=url,
                tier=classify_tier(source, title),
            )
        )
    return items


class _GoogleBlocked(RuntimeError):
    """429/403：被 Google 擋（等下去也沒用）→ 不重試、回空。"""


@retry(
    retry=retry_if_exception_type(httpx.RequestError),  # 只重試暫時性網路錯；被擋/解析空不重試
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    reraise=True,
)
def _get_html(ticker: str) -> str:
    rate_limiter.acquire("google_finance")
    with httpx.Client(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        resp = client.get(_QUOTE_URL.format(ticker=ticker))
    if resp.status_code in (403, 429):
        raise _GoogleBlocked(f"Google Finance blocked ticker={ticker} HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.text


def fetch_symbol_news(symbol: str, *, limit: int = 2, today: date | None = None) -> list[NewsItem]:
    """抓單一台股的 Google Finance 新聞（最新 limit 則）。失敗/無新聞/非台股一律回 []，不拋。"""
    ticker = _google_ticker(symbol)
    if not ticker:
        return []
    try:
        html = _get_html(ticker)
    except Exception as exc:  # noqa: BLE001 — 單檔失敗不阻斷其他（含被擋、網路、retry 耗盡）
        logger.warning("google news fetch %s (%s) failed: %s", symbol, ticker, exc)
        return []
    items = _parse_news(html, symbol, today or date.today())
    if not items:
        # 解析回 0：多為冷門股無新聞；若熱門股長期 0 可能是改版 → log 供監測。
        logger.info("google news %s (%s): 0 items", symbol, ticker)
    return items[:limit]


def fetch_news(symbols: list[str], *, per_symbol: int = 2) -> list[dict[str, Any]]:
    """多檔 Google 新聞 → JSON-safe dict list（欄位對齊 news_loader 輸出）。單檔失敗不阻斷。"""
    today = date.today()
    out: list[NewsItem] = []
    for sym in symbols:
        out.extend(fetch_symbol_news(sym, limit=per_symbol, today=today))
    out.sort(key=lambda x: x.date, reverse=True)
    return [
        {"symbol": n.symbol, "name": n.name, "date": n.date,
         "title": n.title, "source": n.source, "url": n.url, "tier": n.tier,
         "provider": "google"}
        for n in out
    ]
