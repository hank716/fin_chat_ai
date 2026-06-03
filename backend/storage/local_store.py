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
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from config import settings

PARQUET_ROOT = Path(settings.local_storage_path) / "local_parquet"

# 未來日的幽靈列保險絲：trade_date 不可能晚於今天。上游 _assert_reasonable_date 已擋
# TWSE/TPEx 的近未來 glitch；這裡是「跨所有來源」（含 FinMind 正規化列）的最後一道寫入閘，
# 確保任何來源的未來列都進不了 parquet（否則 upsert keep-last 會讓它永久汙染 as_of=max）。
# 留 2 天寬限避免跨時區誤殺當日合法資料（容器 UTC date 可能比台北早 1 天）。
_FUTURE_GRACE_DAYS = 2


def _future_cutoff() -> date:
    return date.today() + timedelta(days=_FUTURE_GRACE_DAYS)

PRICE_COLUMNS = ["trade_date", "open", "high", "low", "close", "volume", "amount", "source"]
CHIP_COLUMNS = ["trade_date", "foreign_net_buy", "trust_net_buy", "dealer_net_buy", "source"]
MARGIN_COLUMNS = ["trade_date", "margin_balance", "short_balance", "source"]


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
    cutoff = _future_cutoff()
    buckets: dict[str, list[Any]] = {}
    for r in rows:
        td = getattr(r, "trade_date", None)
        if isinstance(td, date) and td > cutoff:  # 丟棄未來日幽靈列（任何來源）
            continue
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


def write_margin(rows: Iterable[Any], market: str) -> dict[str, Any]:
    """寫融資融券餘額（MarginRow）到 local_parquet/{market}/_margin/{symbol}.parquet。"""
    return _write_by_symbol(rows, MARGIN_COLUMNS, PARQUET_ROOT / market / "_margin")


def read_prices(symbol: str, market: str) -> pd.DataFrame:
    """讀回單檔 OHLCV（不存在回空 DataFrame）。"""
    path = PARQUET_ROOT / market / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=PRICE_COLUMNS)
    return pd.read_parquet(path)


def read_chip(symbol: str, market: str) -> pd.DataFrame:
    """讀回單檔三大法人買賣超（不存在回空 DataFrame）。"""
    path = PARQUET_ROOT / market / "_chip" / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=CHIP_COLUMNS)
    return pd.read_parquet(path)


def read_margin(symbol: str, market: str) -> pd.DataFrame:
    """讀回單檔融資融券餘額（不存在回空 DataFrame）。"""
    path = PARQUET_ROOT / market / "_margin" / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=MARGIN_COLUMNS)
    return pd.read_parquet(path)


def purge_future_rows(market: str | None = None) -> dict[str, Any]:
    """一次性清掉磁碟上既有的「未來日」幽靈列（價/籌碼/融券）。

    歷史 glitch（_assert_reasonable_date 修補前漏網的近未來日）會以唯一 trade_date 落地、
    upsert keep-last 永不覆蓋，長期汙染 as_of=max(trade_date)。此函式掃描所有 per-symbol
    parquet，移除 trade_date > 今日(留寬限) 的列，回報清理統計。冪等、可重複執行。
    """
    cutoff = pd.Timestamp(_future_cutoff())
    roots = [PARQUET_ROOT / market] if market else [p for p in PARQUET_ROOT.iterdir() if p.is_dir()]
    files_scanned = 0
    files_modified = 0
    rows_removed = 0
    for root in roots:
        for path in root.rglob("*.parquet"):
            files_scanned += 1
            try:
                df = pd.read_parquet(path)
            except Exception:  # noqa: BLE001 — 壞檔跳過
                continue
            if df.empty or "trade_date" not in df.columns:
                continue
            ts = pd.to_datetime(df["trade_date"])
            mask_future = ts > cutoff
            n = int(mask_future.sum())
            if n == 0:
                continue
            kept = df.loc[~mask_future].reset_index(drop=True)
            kept.to_parquet(path, engine="pyarrow", index=False)
            files_modified += 1
            rows_removed += n
    return {
        "cutoff": cutoff.date().isoformat(),
        "files_scanned": files_scanned,
        "files_modified": files_modified,
        "rows_removed": rows_removed,
    }
