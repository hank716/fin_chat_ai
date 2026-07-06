"""WP1.1 除權息事件因子表（spec 017-adjusted-prices）。

單一最大準確度污染源＝TWSE/FinMind 都用原始 close，除息跳空污染所有 return/波動/MA 特徵、
triple-barrier 標籤與回測 P&L。本模組建「事件因子表」，讀取端再據此還原（不重寫既有 parquet、
不並存 adj_close 欄，見全域決策 D2）。

因子定義：`adj_factor = after_price / before_price`（FinMind TaiwanStockDividendResult 的除權息
參考價比）。ex_date 前的所有價乘上 ∏factor 即得還原價（backward 累積，見 WP1.2 讀取端）。

流程：
  build()          — 一次性回補：掃 tw/ parquet 既有個股，逐檔取除權息結果（免費 tier 僅 per-stock，
                     過 rate_limiter；斷點續跑靠 _done checkpoint，遇 quota/ban 停手下輪續）。
  refresh_recent() — 每日增量：全 universe 切 buckets 份、依日序每天刷一份近況（per-stock 限制下
                     的折衷；market-wide 查詢需付費 level）。掛進 morning_brief 回測迴圈 guarded 區。
  read_adj_factors — 見 storage.local_store（讀取層）。

sanity：因子必須 ∈ (0.5, 1.0]，否則丟棄並 log（配息只下調參考價；極端配股才逼近 0.5）。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

import pandas as pd

from storage import local_store
from storage.local_store import ADJ_FACTORS_COLUMNS, ADJ_FACTORS_PATH, PARQUET_ROOT

logger = logging.getLogger(__name__)

TW_MARKET = "tw"
# 回補斷點：已完整取過除權息結果的 symbol（含「無股利」者，避免重跑重打 API）。
_DONE_PATH = PARQUET_ROOT / "_adj_factors_done.json"
# 回補起始日：夠早以涵蓋全部歷史除權息（FinMind 對過早日期回空而非報錯）。
_BACKFILL_START = "2000-01-01"

# sanity 邊界：除權息參考價比。配息只會下修參考價 → 因子 <1；極端高配股才逼近 0.5。
_FACTOR_MIN = 0.5
_FACTOR_MAX = 1.0


def compute_factor(before: Any, after: Any) -> float | None:
    """after/before 還原因子；非數值、非正、或落在 (0.5, 1.0] 之外皆回 None（sanity 丟棄）。"""
    try:
        b = float(before)
        a = float(after)
    except (TypeError, ValueError):
        return None
    if b <= 0 or a <= 0:
        return None
    f = a / b
    if not (_FACTOR_MIN < f <= _FACTOR_MAX):
        return None
    return round(f, 6)


def rows_from_results(symbol: str, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 FinMind TaiwanStockDividendResult 原始列 → 因子表列（過 sanity）。"""
    out: list[dict[str, Any]] = []
    for r in raw:
        d = r.get("date")
        f = compute_factor(r.get("before_price"), r.get("after_price"))
        if not d or f is None:
            if d and r.get("before_price") not in (None, ""):
                logger.debug("adj_factors 丟棄 %s %s（factor sanity）: %s→%s",
                             symbol, d, r.get("before_price"), r.get("after_price"))
            continue
        out.append({"symbol": symbol, "ex_date": pd.Timestamp(d),
                    "adj_factor": f, "source": "finmind"})
    return out


def _tw_symbols() -> list[str]:
    d = PARQUET_ROOT / TW_MARKET
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.parquet"))


def _load_done() -> set[str]:
    if _DONE_PATH.exists():
        try:
            return set(json.loads(_DONE_PATH.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 — 壞 checkpoint 當作空、重跑
            return set()
    return set()


def _save_done(done: set[str]) -> None:
    _DONE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DONE_PATH.write_text(json.dumps(sorted(done)), encoding="utf-8")


def _fetch_symbol(symbol: str, start_date: str) -> list[dict[str, Any]]:
    from data_sources import finmind_loader as fl

    raw = fl.get_dividend_result(symbol, start_date)
    return rows_from_results(symbol, raw)


def build(*, symbols: list[str] | None = None, start_date: str = _BACKFILL_START,
          resume: bool = True, max_symbols: int | None = None) -> dict[str, Any]:
    """一次性回補除權息因子表。斷點續跑：resume=True 時跳過 checkpoint 已完成的 symbol。

    遇 FinMind quota/ban（FinMindBackoff）立即停手（不記為 done），下輪自動續跑。
    """
    from data_sources.finmind_loader import FinMindBackoff

    syms = symbols if symbols is not None else _tw_symbols()
    done = _load_done() if resume else set()
    todo = [s for s in syms if s not in done]
    if max_symbols is not None:
        todo = todo[:max_symbols]

    processed = 0
    events = 0
    stopped: str | None = None
    for sym in todo:
        try:
            rows = _fetch_symbol(sym, start_date)
        except FinMindBackoff as exc:  # 額度/封鎖 → 停手，下輪續（不加入 done）
            stopped = str(exc)
            break
        except Exception as exc:  # noqa: BLE001 — 單檔壞資料不阻斷，記為 done 免重打
            logger.warning("adj_factors 回補略過 %s: %s", sym, exc)
            done.add(sym)
            processed += 1
            continue
        if rows:
            local_store.write_adj_factors(pd.DataFrame(rows))
            events += len(rows)
        done.add(sym)
        processed += 1
        if processed % 50 == 0:
            _save_done(done)

    _save_done(done)
    total = len(local_store.read_adj_factors())
    remaining = len([s for s in syms if s not in done])
    result = {"processed": processed, "events_added": events, "rows_total": total,
              "remaining": remaining, "stopped": stopped}
    logger.info("adj_factors build: %s", result)
    return result


def refresh_recent(*, buckets: int = 14, lookback_days: int = 120) -> dict[str, Any]:
    """每日增量（guarded）：全 universe 切 buckets 份、依日序刷一份近況，upsert 新除權息。

    免費 tier 僅 per-stock → 全掃 2400+ 檔不可行；分桶讓一日只打約 universe/buckets 檔，
    ~buckets 天覆蓋一輪，新除權息在 lookback_days 窗內必被補上（讀取層在還原時容忍此延遲）。
    """
    from data_sources.finmind_loader import FinMindBackoff

    syms = _tw_symbols()
    if not syms:
        return {"refreshed": 0, "events": 0, "bucket": None}
    b = date.today().toordinal() % buckets
    batch = [s for i, s in enumerate(syms) if i % buckets == b]
    start = (date.today() - timedelta(days=lookback_days)).isoformat()

    refreshed = 0
    events = 0
    for sym in batch:
        try:
            rows = _fetch_symbol(sym, start)
        except FinMindBackoff:
            break
        except Exception as exc:  # noqa: BLE001
            logger.debug("adj_factors 增量略過 %s: %s", sym, exc)
            continue
        if rows:
            local_store.write_adj_factors(pd.DataFrame(rows))
            events += len(rows)
        refreshed += 1
    return {"bucket": b, "batch_size": len(batch), "refreshed": refreshed, "events": events}


def read_adj_factors(symbol: str | None = None) -> pd.DataFrame:
    """便利轉發（讀取實作在 storage.local_store）。"""
    return local_store.read_adj_factors(symbol)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="除權息因子表：回補 / 增量 / 統計")
    ap.add_argument("--build", action="store_true", help="一次性回補（斷點續跑）")
    ap.add_argument("--no-resume", action="store_true", help="忽略 checkpoint 從頭跑")
    ap.add_argument("--max-symbols", type=int, default=None, help="本輪最多處理幾檔（分批/測試）")
    ap.add_argument("--refresh", action="store_true", help="跑一次每日增量分桶")
    ap.add_argument("--symbols", nargs="*", help="指定 symbol 清單（預設掃 tw/ 全部）")
    args = ap.parse_args()

    if args.build:
        print(json.dumps(build(symbols=args.symbols, resume=not args.no_resume,
                               max_symbols=args.max_symbols), ensure_ascii=False, indent=2))
    elif args.refresh:
        print(json.dumps(refresh_recent(), ensure_ascii=False, indent=2))
    else:
        df = read_adj_factors()
        print(f"rows={len(df)} symbols={df['symbol'].nunique() if not df.empty else 0} "
              f"path={ADJ_FACTORS_PATH}")


if __name__ == "__main__":
    main()
