"""TW 抓取 → parquet 落地 orchestration（M2 新寫）。

參考 finflow tasks/ingest.py 的抓取流程，但落地改寫成 parquet sink（取代 Postgres upsert），
並切斷對 finflow ORM models 的相依。每個來源獨立 try，單一來源失敗不影響其他來源。

抓取流程：
  prices: TWSE MI_INDEX（歷史可拉）+ TPEx daily（R-1.1b：永遠回今日，不支援歷史）
  chip  : TWSE T86 + TPEx 3itrade（皆支援歷史）
DQ：每列過 PriceRow/ChipRow.is_valid() 才落地（對應 finflow 的品質檢查）。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

from storage import local_store

from . import twse_loader
from .twse_loader import TWSENoDataError

logger = logging.getLogger("ai-market-backend.ingest")

TW_MARKET = "tw"


def _fetch_with_fallback(fetch: Callable[[date], tuple[date, list]], start: date,
                         *, max_back: int = 7) -> tuple[date, list]:
    """抓 start，遇 TWSENoDataError（假日/未公布）往前回退最多 max_back 天。"""
    d = start
    last_exc: Exception | None = None
    for _ in range(max_back):
        try:
            return fetch(d)
        except TWSENoDataError as exc:
            last_exc = exc
            d -= timedelta(days=1)
    raise TWSENoDataError(f"no data in last {max_back} days from {start}") from last_exc


def _dq_filter(rows: list) -> list:
    out = []
    for r in rows:
        ok, _reason = r.is_valid()
        if ok:
            out.append(r)
    return out


def ingest_tw_prices(trade_date: date | None = None) -> dict[str, Any]:
    """抓 TWSE + TPEx 全市場日 OHLCV → 落 parquet。"""
    start = trade_date or date.today()
    result: dict[str, Any] = {"sources": {}}

    actual, twse_rows = _fetch_with_fallback(twse_loader.fetch_twse_daily, start)
    result["trade_date"] = actual.isoformat()
    twse_valid = _dq_filter(twse_rows)
    result["sources"]["twse"] = {
        "fetched": len(twse_rows),
        "valid": len(twse_valid),
        **local_store.write_prices(twse_valid, TW_MARKET),
    }

    # TPEx daily endpoint 永遠回今日（R-1.1b）；以 actual 為準呼叫，失敗不影響 TWSE
    try:
        _, tpex_rows = twse_loader.fetch_tpex_daily(actual)
        tpex_valid = _dq_filter(tpex_rows)
        result["sources"]["tpex"] = {
            "fetched": len(tpex_rows),
            "valid": len(tpex_valid),
            **local_store.write_prices(tpex_valid, TW_MARKET),
        }
    except Exception as exc:  # noqa: BLE001 — 單一來源失敗不阻斷
        logger.warning("tpex prices ingest failed: %s", exc)
        result["sources"]["tpex"] = {"error": str(exc)}

    return result


def ingest_tw_chip(trade_date: date | None = None) -> dict[str, Any]:
    """抓 TWSE + TPEx 三大法人買賣超 → 落 parquet。"""
    start = trade_date or date.today()
    result: dict[str, Any] = {"sources": {}}

    actual, twse_rows = _fetch_with_fallback(twse_loader.fetch_twse_chip, start)
    result["trade_date"] = actual.isoformat()
    twse_valid = _dq_filter(twse_rows)
    result["sources"]["twse"] = {
        "fetched": len(twse_rows),
        "valid": len(twse_valid),
        **local_store.write_chip(twse_valid, TW_MARKET),
    }

    try:
        _, tpex_rows = _fetch_with_fallback(twse_loader.fetch_tpex_chip, actual)
        tpex_valid = _dq_filter(tpex_rows)
        result["sources"]["tpex"] = {
            "fetched": len(tpex_rows),
            "valid": len(tpex_valid),
            **local_store.write_chip(tpex_valid, TW_MARKET),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("tpex chip ingest failed: %s", exc)
        result["sources"]["tpex"] = {"error": str(exc)}

    return result


def ingest_tw_daily(trade_date: date | None = None) -> dict[str, Any]:
    """一次跑完 prices + chip。"""
    return {
        "prices": ingest_tw_prices(trade_date),
        "chip": ingest_tw_chip(trade_date),
    }
