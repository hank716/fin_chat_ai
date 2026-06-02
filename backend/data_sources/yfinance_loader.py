"""yfinance loader（M1 新寫；finflow 無此來源）。

抓美股大盤指數 + BTC 的日 OHLCV，正規化成既有 PriceRow（與 TWSE/FinMind 同型），
落同一個 parquet sink。供 M1 intermarket features 用（美股↔BTC↔台股連動）。

所有對外請求先過 rate_limiter.acquire('yahoo')。yfinance 無金鑰、抓 Yahoo 公開資料。
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any

import yfinance as yf

from . import rate_limiter
from .twse_loader import PriceRow

logger = logging.getLogger("ai-market-backend.yfinance")

# (yfinance symbol, 本系統 symbol, market) — symbol 去掉 ^ 方便當檔名
INTERMARKET_SYMBOLS: list[tuple[str, str, str]] = [
    ("^GSPC", "SP500", "us"),     # S&P 500
    ("^IXIC", "NASDAQ", "us"),    # Nasdaq Composite
    ("^DJI", "DJI", "us"),        # Dow Jones
    ("^SOX", "SOX", "us"),        # 費城半導體（與台股最相關）
    ("^TWII", "TWII", "tw"),      # 台股加權指數（大盤）
    ("BTC-USD", "BTC", "crypto"),
]


def _to_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (ValueError, ArithmeticError):
        return None
    return d if d.is_finite() else None


def fetch_history(yf_symbol: str, symbol: str, *, period: str = "1mo") -> list[PriceRow]:
    """抓單一標的近 period 的日 OHLCV → list[PriceRow]（source='yfinance'）。"""
    rate_limiter.acquire("yahoo")
    df = yf.Ticker(yf_symbol).history(period=period, interval="1d", auto_adjust=False)
    rows: list[PriceRow] = []
    for idx, r in df.iterrows():
        trade_date: date = idx.date()
        vol = r.get("Volume")
        rows.append(
            PriceRow(
                symbol=symbol,
                trade_date=trade_date,
                open=_to_decimal(r.get("Open")),
                high=_to_decimal(r.get("High")),
                low=_to_decimal(r.get("Low")),
                close=_to_decimal(r.get("Close")),
                volume=int(vol) if vol is not None and not _is_nan(vol) else None,
                amount=None,  # yfinance 無成交金額
                source="yfinance",
            )
        )
    return rows


def _is_nan(v: Any) -> bool:
    try:
        return v != v  # NaN != NaN
    except Exception:  # noqa: BLE001
        return False


def fetch_intermarket(*, period: str = "3mo") -> dict[str, list[PriceRow]]:
    """抓全部 intermarket 標的，回 {market: [PriceRow,...]}（DQ 過濾交給 caller/sink）。"""
    by_market: dict[str, list[PriceRow]] = {}
    for yf_symbol, symbol, market in INTERMARKET_SYMBOLS:
        try:
            rows = fetch_history(yf_symbol, symbol, period=period)
            by_market.setdefault(market, []).extend(rows)
        except Exception as exc:  # noqa: BLE001 — 單一標的失敗不阻斷其他
            logger.warning("yfinance fetch %s failed: %s", yf_symbol, exc)
    return by_market
