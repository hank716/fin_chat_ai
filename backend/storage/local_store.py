"""Local parquet sink（M2 新寫，取代 finflow 的 Postgres 落地）。

把抓取層正規化後的 PriceRow / ChipRow 寫進 local parquet（SSOT）：
    storage/local_parquet/{market}/{symbol}.parquet        ← 日 OHLCV
    storage/local_parquet/{market}/_chip/{symbol}.parquet  ← 三大法人買賣超

落地策略：per-symbol 一檔 parquet，依 trade_date upsert（重抓同日覆蓋舊值）。
Decimal → float 落地（parquet 無原生 Decimal 便利型，分析端用 float 足夠）。
時間索引一律用「資料公布日 trade_date」對齊 design_docs（避免 future leakage）。
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from config import settings

PARQUET_ROOT = Path(settings.local_storage_path) / "local_parquet"

PRICE_COLUMNS = ["trade_date", "open", "high", "low", "close", "volume", "amount", "source"]
CHIP_COLUMNS = ["trade_date", "foreign_net_buy", "trust_net_buy", "dealer_net_buy", "source"]


def _to_native(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    return v


def _rows_to_df(rows: Iterable[Any], columns: list[str]) -> pd.DataFrame:
    records = []
    for r in rows:
        d = asdict(r)
        records.append({c: _to_native(d.get(c)) for c in columns})
    df = pd.DataFrame.from_records(records, columns=columns)
    if not df.empty:
        df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def _upsert_parquet(path: Path, df_new: pd.DataFrame) -> int:
    """依 trade_date upsert 寫入 path，回傳該 symbol 落地後總列數。"""
    if df_new.empty:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        df_old = pd.read_parquet(path)
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new
    df = (
        df.drop_duplicates(subset=["trade_date"], keep="last")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    df.to_parquet(path, engine="pyarrow", index=False)
    return len(df)


def _write_by_symbol(rows: Iterable[Any], columns: list[str], subdir: Path) -> dict[str, Any]:
    buckets: dict[str, list[Any]] = {}
    for r in rows:
        buckets.setdefault(r.symbol, []).append(r)
    symbols = 0
    rows_written = 0
    for symbol, sym_rows in buckets.items():
        df = _rows_to_df(sym_rows, columns)
        n = _upsert_parquet(subdir / f"{symbol}.parquet", df)
        if n:
            symbols += 1
            rows_written += len(sym_rows)
    return {"symbols": symbols, "rows_written": rows_written, "path": str(subdir)}


def write_prices(rows: Iterable[Any], market: str) -> dict[str, Any]:
    """寫日 OHLCV（PriceRow）到 local_parquet/{market}/{symbol}.parquet。"""
    return _write_by_symbol(rows, PRICE_COLUMNS, PARQUET_ROOT / market)


def write_chip(rows: Iterable[Any], market: str) -> dict[str, Any]:
    """寫三大法人買賣超（ChipRow）到 local_parquet/{market}/_chip/{symbol}.parquet。"""
    return _write_by_symbol(rows, CHIP_COLUMNS, PARQUET_ROOT / market / "_chip")


def read_prices(symbol: str, market: str) -> pd.DataFrame:
    """讀回單檔 OHLCV（不存在回空 DataFrame）。"""
    path = PARQUET_ROOT / market / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=PRICE_COLUMNS)
    return pd.read_parquet(path)
