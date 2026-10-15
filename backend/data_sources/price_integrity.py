"""上市行情的完整性檢查與自癒（spec 018 US4）。

2026-10-13 追「還原後仍超出漲跌幅限制的跳動」時發現，剩下的多數不是公司行動，而是行情列本身有問題：

1. **缺日**：約 60 個交易日完全沒有上市（TWSE）個股的列，只有上櫃。單日抓取失敗時
   `backfill_tw_market._try` 回空清單、慢爬游標照樣前進，洞就永遠留著；每日刷新只回看近幾天，
   漏掉的那天過了窗口也不會再補（2026-10-08、09-24 都是這樣）。
2. **錯置日**：TWSE 偶發回應的 `date` 與請求不符（觀察到的都落在當月 18 日），舊檢查只擋
   「未來」與「偏離超過 30 天」，於是別天的資料被標成 18 日落地——3189 在 2026-03-18 存的
   其實是 03-04 的行情。標到週末的就成了不存在的交易日。

兩者都讓跨日報酬被當成單日報酬，污染所有 rolling 特徵與前瞻標籤。

`diagnose()` 只讀；`repair()` 會刪除幽靈日的列並重抓有問題的交易日。重抓走既有的
`backfill_window`（已加嚴格日期檢查），upsert 覆蓋，冪等、可重複執行。
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, timedelta
from typing import Any

import pandas as pd

from storage import local_store
from storage.local_store import PARQUET_ROOT

logger = logging.getLogger("ai-market-backend.price_integrity")

TW_MARKET = "tw"
LISTED_SOURCE = "twse"
# 指數（TWII）走 yfinance，偶爾會給出非交易日的列，不能拿來當交易日曆
INDEX_SOURCE = "yfinance"
REPORT_PATH = PARQUET_ROOT / "_price_integrity.json"
# 當日上市列數低於「鄰近交易日中位數」的這個比例，就算缺日
_MISSING_RATIO = 0.5
# 同一天有這麼多檔出現未解釋跳動，才懷疑是整天的行情被錯置（個別跳動多半是真事件）
_SUSPECT_MIN_JUMPS = 5
_BATCH_DATES = 15
_COMMON_STOCK = re.compile(r"[1-9]\d{3}")


def coverage() -> pd.DataFrame:
    """每個日期的列數，分上市（twse）、指數（yfinance）與其他來源（上櫃）。index=date。"""
    listed: Counter[date] = Counter()
    index: Counter[date] = Counter()
    other: Counter[date] = Counter()
    for path in (PARQUET_ROOT / TW_MARKET).glob("*.parquet"):
        try:
            df = pd.read_parquet(path, columns=["trade_date", "source"])
        except Exception as exc:  # noqa: BLE001 — 壞檔交給 scan_corrupt_parquet
            logger.warning("coverage 跳過 %s: %s", path.name, exc)
            continue
        if df.empty:
            continue
        days = pd.to_datetime(df["trade_date"]).dt.date
        is_listed = (df["source"] == LISTED_SOURCE).to_numpy()
        is_index = (df["source"] == INDEX_SOURCE).to_numpy()
        listed.update(days[is_listed])
        index.update(days[is_index])
        other.update(days[~is_listed & ~is_index])
    idx = sorted(set(listed) | set(index) | set(other))
    return pd.DataFrame({"listed": [listed.get(d, 0) for d in idx],
                         "index": [index.get(d, 0) for d in idx],
                         "other": [other.get(d, 0) for d in idx]}, index=idx)


def diagnose(*, since: date | None = None, jumps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """找出幽靈日、上市缺日、疑似錯置日。純讀取。

    `jumps` 是 `adj_factors.find_unexplained_jumps()` 的結果（由呼叫端傳入以免重複掃描）；
    不給就不判錯置日。
    """
    cov = coverage()
    if cov.empty:
        return {"ghost_dates": [], "missing_dates": [], "suspect_dates": []}
    # 上櫃（FinMind 逐檔）沒有錯置日問題，拿它當交易日曆：上櫃沒列而上市或指數有列＝不存在的交易日
    with_other = cov.index[cov["other"] > 0]
    if with_other.empty:  # 沒有上櫃日曆可對照，不做判斷
        return {"ghost_dates": [], "missing_dates": [], "suspect_dates": []}
    lo, hi = with_other.min(), with_other.max()
    in_range = (cov.index >= lo) & (cov.index <= hi)
    stray = (cov["listed"] > 0) | (cov["index"] > 0)
    ghost = [d for d in cov.index[in_range & (cov["other"] == 0) & stray]]

    real = cov[cov["other"] > 0]
    typical = real["listed"].rolling(21, center=True, min_periods=5).median()
    missing = [d for d in real.index[real["listed"] < typical * _MISSING_RATIO]]

    suspect: list[date] = []
    if jumps:
        # 只看普通股：權證、ETN 等本來就沒有 ±10% 的行為，會把每一天都染成可疑
        per_day = Counter(date.fromisoformat(j["date"]) for j in jumps
                          if _COMMON_STOCK.fullmatch(str(j["symbol"])))
        real_days = list(real.index)
        position = {d: i for i, d in enumerate(real_days)}
        for day, n in per_day.items():
            if n < _SUSPECT_MIN_JUMPS or day not in position:
                continue
            # 錯置列在 D 造成一次跳動、D+1 再跳回來；兩天都可能是壞的那一天，一起重抓
            i = position[day]
            suspect.extend(real_days[max(i - 1, 0): i + 1])
        suspect = sorted(set(suspect) - set(missing))

    def keep(days: list[date]) -> list[str]:
        return [d.isoformat() for d in sorted(days) if since is None or d >= since]

    return {"ghost_dates": keep(ghost), "missing_dates": keep(missing), "suspect_dates": keep(suspect)}


def purge_dates(days: list[str]) -> dict[str, int]:
    """刪掉指定日期的所有列（價/籌碼/融資券）。只用於幽靈日——不存在的交易日。"""
    targets = {pd.Timestamp(d) for d in days}
    files_modified = rows_removed = 0
    if not targets:
        return {"files_modified": 0, "rows_removed": 0}
    for path in (PARQUET_ROOT / TW_MARKET).rglob("*.parquet"):
        try:
            df = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("purge_dates 跳過壞檔 %s: %s", path, exc)
            continue
        if df.empty or "trade_date" not in df.columns:
            continue
        mask = pd.to_datetime(df["trade_date"]).isin(targets)
        n = int(mask.sum())
        if n == 0:
            continue
        local_store.write_parquet_atomic(df.loc[~mask].reset_index(drop=True), path)
        files_modified += 1
        rows_removed += n
    return {"files_modified": files_modified, "rows_removed": rows_removed}


def repair(*, lookback_days: int | None = None, max_dates: int | None = None,
           jumps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """刪幽靈日、重抓缺日與疑似錯置日。遇 TWSE 反爬封鎖立即停手，下次續跑（冪等）。"""
    from data_sources.backfill_tw_market import backfill_dates
    from data_sources.twse_loader import TWSEBlockedError

    since = date.today() - timedelta(days=lookback_days) if lookback_days else None
    found = diagnose(since=since, jumps=jumps)
    purged = purge_dates(found["ghost_dates"])
    todo = sorted(set(found["missing_dates"]) | set(found["suspect_dates"]), reverse=True)
    if max_dates is not None:
        todo = todo[:max_dates]
    refetched: list[str] = []
    stopped: str | None = None
    # 分批：一批內先收齊再一次寫入（省掉逐日重寫全部檔案），批與批之間各自落地，
    # 中途被封鎖時已完成的批次不會白做。
    for i in range(0, len(todo), _BATCH_DATES):
        batch = todo[i:i + _BATCH_DATES]
        try:
            backfill_dates([date.fromisoformat(day) for day in batch])
        except TWSEBlockedError as exc:
            stopped = f"twse_blocked: {exc}"
            break
        except Exception as exc:  # noqa: BLE001 — 單批失敗不阻斷其餘批次
            logger.warning("price_integrity 重抓 %s..%s 失敗: %s", batch[0], batch[-1], exc)
            continue
        refetched.extend(batch)
    after = diagnose(since=since)
    result = {
        "generated_at": date.today().isoformat(),
        "found": {k: len(v) for k, v in found.items()},
        "purged": purged, "refetched": len(refetched), "stopped": stopped,
        "still_missing": after["missing_dates"], "still_ghost": after["ghost_dates"],
    }
    REPORT_PATH.write_text(json.dumps({**result, "detail": found}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    logger.info("price_integrity repair: %s", result)
    return result


def main() -> None:
    import sys

    logging.basicConfig(level=logging.INFO)
    if "--repair" in sys.argv:
        from processor import adj_factors

        jumps = adj_factors.find_unexplained_jumps()
        print(json.dumps(repair(jumps=jumps), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(diagnose(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
