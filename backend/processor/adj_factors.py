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

sanity（spec 018 修訂）：一般配息的因子落在 (0.5, 1.0]，直接採信。落在此範圍外的事件——
大比例配股、股票分割/面額變更（因子遠小於 0.5）、減資（因子大於 1）——**不再丟棄**，改以
實際股價驗證：因子須與「除權息日開盤 ÷ 前一交易日收盤」相符才採用（`confirm_with_prices`）。
原本的固定下限 0.5 把這些事件全部擋掉，導致 6669（2026-09-02，因子 0.335）這類個股出現
未還原的假跌幅，污染排行、訓練標籤與回測。

三個來源，以 `source` 欄區分（同一天可並存，還原時連乘）：
  finmind            — TaiwanStockDividendResult（除權息；逐檔）
  finmind_split      — TaiwanStockSplitPrice（分割/反分割/面額變更；可全市場一次查）
  finmind_reduction  — TaiwanStockCapitalReductionReferencePrice（減資；免費層僅能逐檔）

  repair()           — 掃還原後仍超出漲跌幅限制的單日跳動，只對這些個股補抓上述來源，
                       剩下解釋不了的落地成清單供稽核（多半是壞資料或無漲跌幅限制的交易日）。
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

# 一般配息的因子範圍：配息只會下修參考價 → 因子 <1；落在此範圍內直接採信。
_FACTOR_MIN = 0.5
_FACTOR_MAX = 1.0
# 範圍外的事件（大比例配股、分割、面額變更、減資）須經股價驗證；這是連驗證都不做的絕對界線。
_WIDE_MIN = 0.001
_WIDE_MAX = 1000.0
# 股價驗證容忍：除權息日開盤價距參考價最多 ±10%（漲跌幅限制），留餘裕取 ±25%。
_CONFIRM_TOLERANCE = 1.25

SOURCE_DIVIDEND = "finmind"
SOURCE_SPLIT = "finmind_split"
SOURCE_REDUCTION = "finmind_reduction"
_UNEXPLAINED_PATH = PARQUET_ROOT / "_adj_unexplained_jumps.json"


def _is_classic(factor: float) -> bool:
    return _FACTOR_MIN < factor <= _FACTOR_MAX


def compute_factor(before: Any, after: Any, *, wide: bool = False) -> float | None:
    """after/before 還原因子；非數值、非正回 None。

    wide=False（預設）：只收一般配息範圍 (0.5, 1.0]。
    wide=True：範圍外也回傳，**呼叫端必須再過 `confirm_with_prices`**。
    """
    try:
        b = float(before)
        a = float(after)
    except (TypeError, ValueError):
        return None
    if b <= 0 or a <= 0:
        return None
    f = a / b
    if _is_classic(f):
        return round(f, 6)
    if wide and _WIDE_MIN < f < _WIDE_MAX:
        return round(f, 6)
    return None


def _rows(symbol: str, raw: list[dict[str, Any]], source: str,
          before_key: str, after_key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in raw:
        d = r.get("date")
        f = compute_factor(r.get(before_key), r.get(after_key), wide=True)
        if not d or f is None:
            continue
        out.append({"symbol": symbol, "ex_date": pd.Timestamp(d), "adj_factor": f, "source": source})
    return out


def rows_from_results(symbol: str, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FinMind TaiwanStockDividendResult 原始列 → 因子表列。範圍外者尚待股價驗證。"""
    return _rows(symbol, raw, SOURCE_DIVIDEND, "before_price", "after_price")


def rows_from_splits(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FinMind TaiwanStockSplitPrice（分割/反分割/面額變更；全市場）→ 因子表列。"""
    out: list[dict[str, Any]] = []
    for r in raw:
        if r.get("stock_id"):
            out.extend(_rows(str(r["stock_id"]), [r], SOURCE_SPLIT, "before_price", "after_price"))
    return out


def rows_from_reductions(symbol: str, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FinMind TaiwanStockCapitalReductionReferencePrice → 因子表列（減資後參考價 ÷ 減資前收盤）。"""
    return _rows(symbol, raw, SOURCE_REDUCTION,
                 "ClosingPriceonTheLastTradingDay", "PostReductionReferencePrice")


def confirm_with_prices(rows: list[dict[str, Any]],
                        prices: pd.DataFrame | None = None) -> list[dict[str, Any]]:
    """一般配息範圍內的列直接放行；範圍外的列須與實際股價相符才保留。

    比對「除權息日開盤 ÷ 前一交易日收盤」（皆為原始名目價）與因子：兩者之比須在
    ±25% 內。開盤價距參考價受 ±10% 漲跌幅限制，所以真事件一定對得上；資料來源填錯、
    或事件日其實沒有交易的列會對不上而被丟棄。**缺價（事件日行情尚未入庫）也丟棄**——
    寧可晚一輪補上，不可把未經驗證的大倍率因子套到整段歷史。`prices` 供測試注入。
    """
    kept: list[dict[str, Any]] = []
    cache: dict[str, pd.DataFrame] = {}
    for row in rows:
        factor = float(row["adj_factor"])
        if _is_classic(factor):
            kept.append(row)
            continue
        sym = row["symbol"]
        if sym not in cache:
            px = prices if prices is not None else local_store.read_prices(sym, TW_MARKET)
            px = px.copy()
            if not px.empty:
                px["trade_date"] = pd.to_datetime(px["trade_date"])
                px = px.sort_values("trade_date").reset_index(drop=True)
            cache[sym] = px
        px = cache[sym]
        ex = pd.Timestamp(row["ex_date"])
        on = px[px["trade_date"] == ex] if not px.empty else px
        before = px[px["trade_date"] < ex] if not px.empty else px
        if on.empty or before.empty:
            logger.info("adj_factors 待驗證（缺 %s 當日或前一日行情）：%s factor=%s", ex.date(), sym, factor)
            continue
        first = on.iloc[0]
        opened = first.get("open")
        ref = float(opened) if pd.notna(opened) and float(opened) > 0 else float(first["close"])
        actual = ref / float(before.iloc[-1]["close"])
        if 1 / _CONFIRM_TOLERANCE <= factor / actual <= _CONFIRM_TOLERANCE:
            kept.append(row)
        else:
            logger.warning("adj_factors 丟棄 %s %s：因子 %s 與實際股價比 %.4f 不符（來源 %s）",
                           sym, ex.date(), factor, actual, row["source"])
    return kept


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
    return confirm_with_prices(rows_from_results(symbol, raw))


def refresh_splits(start_date: str = _BACKFILL_START) -> dict[str, Any]:
    """分割/反分割/面額變更：FinMind 允許不帶個股全市場查，一次請求涵蓋全部。"""
    from data_sources import finmind_loader as fl

    rows = confirm_with_prices(rows_from_splits(fl.get_split_price(start_date)))
    if rows:
        local_store.write_adj_factors(pd.DataFrame(rows))
    return {"events": len(rows)}


def find_unexplained_jumps(threshold: float = 0.25, *, since: str | None = None,
                           symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """還原價序列裡仍超出漲跌幅限制的單日跳動（台股 ±10%）＝還沒被還原的公司行動或壞資料。

    門檻預設 25%：留給「上市首五日無漲跌幅」與停牌後補漲跌的正常情況一點空間，
    代價是 1.1–1.25 倍的小幅減資抓不到（已知限制）。每檔前 10 根不看（新掛牌）。
    """
    out: list[dict[str, Any]] = []
    cutoff = pd.Timestamp(since) if since else None
    for sym in (symbols if symbols is not None else _tw_symbols()):
        px = local_store.read_prices(sym, TW_MARKET, adjusted=True)
        if len(px) <= 11:
            continue
        px = px.copy()
        px["trade_date"] = pd.to_datetime(px["trade_date"])
        px = px.sort_values("trade_date").reset_index(drop=True)
        ret = px["close"].astype(float).pct_change()
        for i in ret.index[(ret.abs() > threshold) & (ret.index > 10)]:
            day = px.loc[i, "trade_date"]
            if cutoff is not None and day < cutoff:
                continue
            out.append({"symbol": sym, "date": day.date().isoformat(), "return_pct": round(float(ret[i]) * 100, 1)})
    return out


def repair(*, threshold: float = 0.25, since: str | None = None,
           max_symbols: int | None = None) -> dict[str, Any]:
    """補上漏掉的公司行動：先全市場分割表，再只對「還有異常跳動」的個股逐檔補抓。

    逐檔查詢受每小時額度限制，所以不全掃——下跌的跳動查除權息結果（大比例配股），
    上漲的查減資。補完重掃一次，解釋不了的落地成清單（`_adj_unexplained_jumps.json`）。
    """
    from data_sources import finmind_loader as fl
    from data_sources.finmind_loader import FinMindBackoff

    added = 0
    stopped: str | None = None
    try:
        added += refresh_splits()["events"]
    except FinMindBackoff as exc:
        stopped = str(exc)
    jumps = find_unexplained_jumps(threshold, since=since)
    before_n = len(jumps)
    by_symbol: dict[str, set[str]] = {}
    for j in jumps:
        by_symbol.setdefault(j["symbol"], set()).add("down" if j["return_pct"] < 0 else "up")
    todo = sorted(by_symbol)[:max_symbols] if max_symbols is not None else sorted(by_symbol)
    queried = 0
    for sym in todo:
        if stopped:
            break
        rows: list[dict[str, Any]] = []
        try:
            if "down" in by_symbol[sym]:
                rows += rows_from_results(sym, fl.get_dividend_result(sym, _BACKFILL_START))
            if "up" in by_symbol[sym]:
                rows += rows_from_reductions(sym, fl.get_capital_reduction(sym, _BACKFILL_START))
        except FinMindBackoff as exc:
            stopped = str(exc)
            break
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不阻斷
            logger.warning("adj_factors repair 略過 %s: %s", sym, exc)
            continue
        queried += 1
        rows = confirm_with_prices(rows)
        if rows:
            local_store.write_adj_factors(pd.DataFrame(rows))
            added += len(rows)
    remaining = find_unexplained_jumps(threshold, since=since, symbols=sorted(by_symbol))
    _UNEXPLAINED_PATH.write_text(json.dumps(
        {"generated_at": date.today().isoformat(), "threshold": threshold, "since": since,
         "jumps": remaining}, ensure_ascii=False, indent=1), encoding="utf-8")
    result = {"jumps_before": before_n, "symbols_queried": queried, "events_written": added,
              "jumps_remaining": len(remaining), "stopped": stopped}
    logger.info("adj_factors repair: %s", result)
    return result


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
    # 分割/面額變更（全市場一次請求）＋近 10 日異常跳動的針對性補抓：新事件隔日即還原，
    # 不必等分桶輪到那一檔。
    repaired: dict[str, Any] = {}
    try:
        repaired = repair(since=(date.today() - timedelta(days=14)).isoformat(), max_symbols=30)
    except Exception as exc:  # noqa: BLE001 — 增量維護不得中斷晨報回測迴圈
        logger.warning("adj_factors repair（增量）失敗: %s", exc)
    return {"bucket": b, "batch_size": len(batch), "refreshed": refreshed, "events": events,
            "repair": repaired}


def read_adj_factors(symbol: str | None = None) -> pd.DataFrame:
    """便利轉發（讀取實作在 storage.local_store）。"""
    return local_store.read_adj_factors(symbol)


def main() -> None:
    import argparse
    import sys

    if "--repair" in sys.argv:          # 補抓漏掉的大比例配股/分割/減資（只查還有異常跳動的個股）
        logging.basicConfig(level=logging.INFO)
        print(json.dumps(repair(), ensure_ascii=False, indent=1))
        return

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
