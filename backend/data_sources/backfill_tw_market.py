"""全台股市場級歷史回補（M8-2）。

用 TWSE/TPEx「單日=全市場」端點，loop 交易日收集後**每 symbol 寫一次** parquet
（避免逐日 upsert 的 O(天數²) 重寫）。取代 FinMind per-stock（全市場 2000+ 檔不可能 per-stock）。

來源與歷史支援：
  TWSE 價(MI_INDEX) / 籌(T86) / 資券(MI_MARGN)：皆支援歷史 → loop 回補。
  TPEx 籌(3itrade)：支援歷史 → loop。
  TPEx 價(daily)：**只回當日、不支援歷史** → 只抓今日（上櫃價歷史靠每日 cron 往後累積）。

CLI：python -m data_sources.backfill_tw_market [days]
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from typing import Any

from trading_calendar import is_tw_trading_day
from storage import local_store

from . import twse_loader
from .ingest import TW_MARKET, _dq_filter
from .twse_loader import TWSENoDataError

logger = logging.getLogger("ai-market-backend.backfill_market")


def _try(fetch, d: date) -> list:
    try:
        _actual, rows = fetch(d)
        return _dq_filter(rows)
    except TWSENoDataError:
        return []
    except Exception as exc:  # noqa: BLE001 — 單來源單日失敗不阻斷
        logger.warning("%s %s 失敗: %s", getattr(fetch, "__name__", fetch), d, exc)
        return []


def backfill_window(start: date, end: date, *, include_tpex_today: bool = False) -> dict[str, Any]:
    """回補 [start, end] 區間的全市場 TWSE/TPEx 價/籌/券（單日端點，皆支援歷史，不打 FinMind）。

    include_tpex_today=True 時額外抓「今日」TPEx 上櫃價（該端點只回當日、無歷史）——僅每日刷新用。
    歷史慢爬請留 False（上櫃價歷史改由 FinMind per-stock 另爬，見 data_sources.history_crawl）。
    """
    prices: list = []
    chips: list = []
    margins: list = []
    trading_days = 0

    d = end
    while d >= start:
        if is_tw_trading_day(d):
            trading_days += 1
            prices += _try(twse_loader.fetch_twse_daily, d)      # TWSE 全市場價（歷史）
            chips += _try(twse_loader.fetch_twse_chip, d)        # TWSE 三大法人（歷史）
            margins += _try(twse_loader.fetch_twse_margin, d)    # TWSE 融資券（歷史）
            chips += _try(twse_loader.fetch_tpex_chip, d)        # TPEx 三大法人（歷史）
            margins += _try(twse_loader.fetch_tpex_margin, d)    # TPEx 融資券（歷史）
        d -= timedelta(days=1)

    # TPEx 價：只回當日（無歷史），抓今日當起點（僅每日刷新；歷史慢爬不抓）
    if include_tpex_today:
        prices += _try(twse_loader.fetch_tpex_daily, end)

    # 過濾到 universe（TPEx daily 會夾帶大量權證等非清單標的，否則會產生數千個垃圾 parquet）
    import universe
    keep = universe.watchlist_symbols()
    prices = [r for r in prices if r.symbol in keep]
    chips = [r for r in chips if r.symbol in keep]
    margins = [r for r in margins if r.symbol in keep]

    # 每 symbol 一次性寫入（write_* 內部依 symbol 分桶、依 trade_date upsert）
    p = local_store.write_prices(prices, TW_MARKET)
    c = local_store.write_chip(chips, TW_MARKET)
    m = local_store.write_margin(margins, TW_MARKET)

    summary = {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "trading_days": trading_days},
        "prices": {"rows": len(prices), "symbols": p["symbols"]},
        "chips": {"rows": len(chips), "symbols": c["symbols"]},
        "margins": {"rows": len(margins), "symbols": m["symbols"]},
    }
    logger.info("區間回補完成: %s", summary)
    return summary


def backfill_market(days: int = 90) -> dict[str, Any]:
    """每日刷新用：回補近 days 天全市場（含今日 TPEx 上櫃價）。委派 backfill_window。"""
    end = date.today()
    return backfill_window(end - timedelta(days=days), end, include_tpex_today=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    import json

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    print(json.dumps(backfill_market(n), ensure_ascii=False, indent=2))
