"""台股 watchlist 歷史回補（M2-report step B）。

用 FinMind per-stock 抓整段歷史（上市+上櫃通吃，每檔一次請求），DQ 後落 parquet：
  價格 → local_parquet/tw/{symbol}.parquet
  籌碼 → local_parquet/tw/_chip/{symbol}.parquet

為何不用 TWSE 日迴圈：TWSE MI_INDEX 抓不到上櫃、TPEx daily 不支援歷史；
FinMind 個股級可一次回整段、兩市通吃，48 檔約 96 次請求（< 600/hr）。

CLI：python -m data_sources.backfill_tw [days]
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from typing import Any

import universe
from storage import local_store

from . import finmind_loader
from .ingest import TW_MARKET, _dq_filter

logger = logging.getLogger("ai-market-backend.backfill_tw")


def backfill_watchlist(days: int = 90) -> dict[str, Any]:
    """回補 watchlist 近 days 日的價格 + 籌碼。"""
    end = date.today()
    start = end - timedelta(days=days)
    start_s, end_s = start.isoformat(), end.isoformat()
    symbols = sorted(universe.watchlist_symbols())

    price_ok = price_fail = chip_ok = chip_fail = 0
    errors: dict[str, str] = {}

    for sym in symbols:
        # 價格
        try:
            rows = _dq_filter(finmind_loader.fetch_stock_prices_normalized(sym, start_s, end_s))
            if rows:
                local_store.write_prices(rows, TW_MARKET)
                price_ok += 1
            else:
                price_fail += 1
                logger.warning("backfill prices %s: 0 valid rows", sym)
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不阻斷其他
            price_fail += 1
            errors[f"price:{sym}"] = str(exc)
            logger.warning("backfill prices %s failed: %s", sym, exc)

        # 籌碼
        try:
            rows = _dq_filter(finmind_loader.fetch_stock_chip_normalized(sym, start_s, end_s))
            if rows:
                local_store.write_chip(rows, TW_MARKET)
                chip_ok += 1
            else:
                chip_fail += 1
                logger.warning("backfill chip %s: 0 valid rows", sym)
        except Exception as exc:  # noqa: BLE001
            chip_fail += 1
            errors[f"chip:{sym}"] = str(exc)
            logger.warning("backfill chip %s failed: %s", sym, exc)

    summary = {
        "window": {"start": start_s, "end": end_s, "days": days},
        "symbols": len(symbols),
        "price": {"ok": price_ok, "fail": price_fail},
        "chip": {"ok": chip_ok, "fail": chip_fail},
        "errors": errors,
    }
    logger.info("台股回補完成: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    import json

    print(json.dumps(backfill_watchlist(n), ensure_ascii=False, indent=2))
