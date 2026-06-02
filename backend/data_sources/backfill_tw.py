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

    counts = {"price": [0, 0], "chip": [0, 0], "margin": [0, 0]}  # [ok, fail]
    errors: dict[str, str] = {}

    def _run(kind: str, fetch, write) -> None:
        try:
            rows = _dq_filter(fetch(sym, start_s, end_s))
            if rows:
                write(rows, TW_MARKET)
                counts[kind][0] += 1
            else:
                counts[kind][1] += 1
                logger.warning("backfill %s %s: 0 valid rows", kind, sym)
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不阻斷其他
            counts[kind][1] += 1
            errors[f"{kind}:{sym}"] = str(exc)
            logger.warning("backfill %s %s failed: %s", kind, sym, exc)

    for sym in symbols:
        _run("price", finmind_loader.fetch_stock_prices_normalized, local_store.write_prices)
        _run("chip", finmind_loader.fetch_stock_chip_normalized, local_store.write_chip)
        _run("margin", finmind_loader.fetch_stock_margin_normalized, local_store.write_margin)

    summary = {
        "window": {"start": start_s, "end": end_s, "days": days},
        "symbols": len(symbols),
        "price": {"ok": counts["price"][0], "fail": counts["price"][1]},
        "chip": {"ok": counts["chip"][0], "fail": counts["chip"][1]},
        "margin": {"ok": counts["margin"][0], "fail": counts["margin"][1]},
        "errors": errors,
    }
    logger.info("台股回補完成: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    import json

    print(json.dumps(backfill_watchlist(n), ensure_ascii=False, indent=2))
