"""Local parquet sink（M2 新寫，取代 finflow 的 Postgres 落地）。

把抓取層正規化後的 PriceRow / ChipRow 寫進 local parquet（SSOT）：
    storage/local_parquet/{market}/{symbol}.parquet        ← 日 OHLCV
    storage/local_parquet/{market}/_chip/{symbol}.parquet  ← 三大法人買賣超

落地策略：per-symbol 一檔 parquet，依 trade_date upsert（重抓同日覆蓋舊值）。
Decimal → float 落地（parquet 無原生 Decimal 便利型，分析端用 float 足夠）。
時間索引一律用「資料公布日 trade_date」對齊 design_docs（避免 future leakage）。
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import pyarrow as pa

from config import settings

logger = logging.getLogger("ai-market-backend.local_store")

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

# 除權息事件因子表（spec 017 / WP1.1）：市場級單檔，key=(symbol, ex_date)。刻意放在
# local_parquet 根目錄（非 tw/ 內），才不會被 training_set._build_big 的 tw/*.parquet glob
# 誤當成一檔個股掃到。schema：symbol / ex_date / adj_factor / source。
ADJ_FACTORS_COLUMNS = ["symbol", "ex_date", "adj_factor", "source"]
ADJ_FACTORS_PATH = PARQUET_ROOT / "tw_adj_factors.parquet"


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


# ── parquet I/O 保險絲（2026-08-30 事故）────────────────────────────────────────
# 容器 rebuild 把 `df.to_parquet(path)` 砍在半路，目的檔被截成「有 PAR1 開頭、沒 PAR1 結尾」的
# 死檔。讀它的 read_margin 拋 ArrowInvalid 往上炸掉整個 build_tw_features（＝晨報）；而寫入端
# _upsert_parquet 讀舊檔那行也炸 → 壞檔連覆寫自救都做不到，變成死結。兩道解法並行：
#   寫入改原子（不再產生半截檔）、讀取遇壞檔隔離改名並回空（自癒 + 留證）。
_TMP_SUFFIX = ".tmp-"          # 刻意不以 .parquet 結尾，免得被 rglob("*.parquet") 當成資料檔掃到
_CORRUPT_SUFFIX = ".corrupt-"


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    """原子寫 parquet：同目錄寫 tmp → fsync → os.replace。

    目的檔在任何瞬間都是「完整舊版」或「完整新版」，程序被 SIGKILL（容器停機）也不會留半截檔。
    tmp 必須與目的檔同目錄（同一 filesystem），os.replace 才具原子性。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名帶 pid + 隨機碼：backend 是多執行緒（背景慢爬 + threadpool），
    # 兩條執行緒同時寫同一檔時不能共用同一個 tmp，否則彼此把對方的半成品寫花。
    tmp = path.with_name(f"{path.name}{_TMP_SUFFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        df.to_parquet(tmp, engine="pyarrow", index=False)
        try:
            with open(tmp, "rb+") as fh:   # 先把 tmp 真正落磁碟，斷電也不會換上半截檔
                os.fsync(fh.fileno())
        except OSError as exc:             # 特殊 filesystem 不支援 fsync：仍可靠 replace 保原子
            logger.debug("fsync %s 失敗（略過，仍走 replace）: %s", tmp, exc)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)        # replace 成功後 tmp 已不存在；失敗則清掉殘檔


def _quarantine_corrupt(path: Path) -> None:
    """把壞檔改名到 <name>.corrupt-<ts>：留證可鑑識，同時讓下次寫入視為「檔不存在」而重建。"""
    if not path.exists():
        return
    dest = path.with_name(f"{path.name}{_CORRUPT_SUFFIX}{int(time.time())}")
    try:
        os.replace(path, dest)
        logger.error("parquet 壞檔已隔離：%s → %s（本次回空資料，下次寫入會重建）", path, dest.name)
    except OSError as exc:
        logger.error("parquet 壞檔 %s 隔離失敗（仍回空資料）: %s", path, exc)


def read_parquet_safe(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    """讀 parquet；檔案損毀就隔離改名 + 回空 DataFrame，不讓單一壞檔炸掉整條管線。

    只捕「檔案打不開」這類 I/O 例外（ArrowInvalid / OSError）——schema 不合或邏輯錯誤照樣往上拋，
    不把真 bug 吞成「資料默默消失」。
    """
    try:
        return pd.read_parquet(path)
    except (pa.ArrowInvalid, OSError) as exc:
        logger.error("parquet 讀取失敗 %s: %s", path, exc)
        _quarantine_corrupt(path)
        return pd.DataFrame(columns=columns or [])


def _upsert_parquet(path: Path, df_new: pd.DataFrame) -> int:
    """依 trade_date upsert 寫入 path，回傳該 symbol 落地後總列數。"""
    if df_new.empty:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # 壞檔在此會被隔離並回空 → 本次寫入等同重建，不再讓單一死檔卡住整批 symbol 的落地。
        df_old = read_parquet_safe(path, list(df_new.columns))
        df = pd.concat([df_old, df_new], ignore_index=True) if not df_old.empty else df_new
    else:
        df = df_new
    df = (
        df.drop_duplicates(subset=["trade_date"], keep="last")
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    write_parquet_atomic(df, path)
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


def read_prices(symbol: str, market: str, *, adjusted: bool = False) -> pd.DataFrame:
    """讀回單檔 OHLCV（不存在回空 DataFrame）。

    adjusted=True：對 OHLC 做除權息 backward 累積還原（spec 017 / WP1.2），使 return/波動/MA/
    標籤跨除權息日連續（消除假跳空）。**顯示價/觸價判定應用 adjusted=False 的原始名目價**（D2）。
    """
    path = PARQUET_ROOT / market / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=PRICE_COLUMNS)
    df = read_parquet_safe(path, PRICE_COLUMNS)
    if adjusted and not df.empty:
        df = _apply_adjustment(df, symbol)
    return df


def _apply_adjustment(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """OHLC 除權息 backward 累積還原：`adj[t] = raw[t] × ∏{ex_date > t} factor`。

    ex_date 當日及之後的價維持原始（該日已是 after_price）；ex_date 之前的價往回累乘各後續因子，
    讓跨除權息日的報酬連續。volume/amount 不還原（報酬/波動運算不需）。無因子（指數/ETF/不配息）
    原樣返回。
    """
    factors = read_adj_factors(symbol)
    if factors.empty:
        return df
    df = df.copy()
    td = pd.to_datetime(df["trade_date"]).to_numpy()
    f = factors.sort_values("ex_date")
    ex = pd.to_datetime(f["ex_date"]).to_numpy()
    cum = np.cumprod(f["adj_factor"].astype(float).to_numpy())
    total = float(cum[-1]) if len(cum) else 1.0
    idx = np.searchsorted(ex, td, side="right")           # ∏{ex_date <= t} 的項數
    prev_cum = np.where(idx > 0, cum[np.clip(idx - 1, 0, len(cum) - 1)], 1.0)
    mult = total / prev_cum                                # = ∏{ex_date > t} factor
    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = df[col].astype(float) * mult
    return df


def read_chip(symbol: str, market: str) -> pd.DataFrame:
    """讀回單檔三大法人買賣超（不存在回空 DataFrame）。"""
    path = PARQUET_ROOT / market / "_chip" / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=CHIP_COLUMNS)
    return read_parquet_safe(path, CHIP_COLUMNS)


def read_margin(symbol: str, market: str) -> pd.DataFrame:
    """讀回單檔融資融券餘額（不存在回空 DataFrame）。"""
    path = PARQUET_ROOT / market / "_margin" / f"{symbol}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=MARGIN_COLUMNS)
    return read_parquet_safe(path, MARGIN_COLUMNS)


def read_adj_factors(symbol: str | None = None) -> pd.DataFrame:
    """讀回除權息因子表（不存在回空 DataFrame）。給 symbol 則只回該檔、依 ex_date 排序。"""
    if not ADJ_FACTORS_PATH.exists():
        return pd.DataFrame(columns=ADJ_FACTORS_COLUMNS)
    df = read_parquet_safe(ADJ_FACTORS_PATH, ADJ_FACTORS_COLUMNS)
    if symbol is not None:
        df = df[df["symbol"] == symbol].sort_values("ex_date").reset_index(drop=True)
    return df


def write_adj_factors(df_new: pd.DataFrame) -> int:
    """依 (symbol, ex_date) upsert 除權息因子表，回傳落地後總列數。"""
    if df_new is None or df_new.empty:
        return len(read_adj_factors())
    df_new = df_new[ADJ_FACTORS_COLUMNS].copy()
    df_new["ex_date"] = pd.to_datetime(df_new["ex_date"])
    ADJ_FACTORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ADJ_FACTORS_PATH.exists():
        old = read_parquet_safe(ADJ_FACTORS_PATH, ADJ_FACTORS_COLUMNS)
        df = pd.concat([old, df_new], ignore_index=True) if not old.empty else df_new
    else:
        df = df_new
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    df = (
        df.drop_duplicates(subset=["symbol", "ex_date"], keep="last")
        .sort_values(["symbol", "ex_date"])
        .reset_index(drop=True)
    )
    write_parquet_atomic(df, ADJ_FACTORS_PATH)
    return len(df)


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
            except Exception as exc:  # noqa: BLE001 — 壞檔跳過，但要留聲音
                # 2026-08-30：這裡原本靜默 continue，害被截斷的 5530 融資券檔潛伏到炸掉晨報
                # 才曝光。清幽靈列不該順手改壞檔（那是 scan_corrupt_parquet 的事），但至少喊一聲。
                logger.warning("purge 掃描跳過壞檔 %s（用 scan_corrupt_parquet 處理）: %s", path, exc)
                continue
            if df.empty or "trade_date" not in df.columns:
                continue
            ts = pd.to_datetime(df["trade_date"])
            mask_future = ts > cutoff
            n = int(mask_future.sum())
            if n == 0:
                continue
            kept = df.loc[~mask_future].reset_index(drop=True)
            write_parquet_atomic(kept, path)
            files_modified += 1
            rows_removed += n
    return {
        "cutoff": cutoff.date().isoformat(),
        "files_scanned": files_scanned,
        "files_modified": files_modified,
        "rows_removed": rows_removed,
    }


def scan_corrupt_parquet(quarantine: bool = False) -> dict[str, Any]:
    """掃 PARQUET_ROOT 下所有 parquet，回報（可選隔離）打不開的死檔。冪等、可重複執行。

    2026-08-30 事故的教訓：purge_future_rows 內建 `except: continue` 會默默跳過壞檔，
    所以一個被截斷的 5530 融資券檔潛伏到炸掉晨報才被發現。這支是專門把它們照出來的燈。
    quarantine=False（預設）＝純唯讀報告；True 才改名隔離（下次寫入自動重建）。
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 — 只在維運掃描時才需要，不進 import 熱路徑

    corrupt: list[dict[str, Any]] = []
    files_scanned = 0
    if not PARQUET_ROOT.exists():
        return {"files_scanned": 0, "corrupt": [], "quarantined": 0}
    for path in PARQUET_ROOT.rglob("*.parquet"):
        files_scanned += 1
        try:
            pq.ParquetFile(path)                  # 只讀 footer，不載入資料，全庫掃很快
        except (pa.ArrowInvalid, OSError) as exc:
            corrupt.append({"path": str(path), "size": path.stat().st_size, "error": str(exc)[:120]})
    quarantined = 0
    if quarantine:
        for item in corrupt:
            p = Path(item["path"])
            _quarantine_corrupt(p)
            if not p.exists():
                quarantined += 1
    return {"files_scanned": files_scanned, "corrupt": corrupt, "quarantined": quarantined}
