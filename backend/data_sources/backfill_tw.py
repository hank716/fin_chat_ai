"""台股 watchlist 歷史回補（M2-report step B）。

用 FinMind per-stock 抓整段歷史（上市+上櫃通吃，每檔一次請求），DQ 後落 parquet：
  價格 → local_parquet/tw/{symbol}.parquet
  籌碼 → local_parquet/tw/_chip/{symbol}.parquet

為何不用 TWSE 日迴圈：TWSE MI_INDEX 抓不到上櫃、TPEx daily 不支援歷史；
FinMind 個股級可一次回整段、兩市通吃。

⚠️ M8 起 watchlist = 全台股（~2700 檔），逐檔抓會是 ~8000 次 FinMind 請求，
遠超免費版 600/hr 必被封 IP。全市場「初始化」請改用 backfill_tw_market（TWSE/TPEx
全市場單日端點，不走 FinMind）。本模組現適用於：少量指定標的回補、或 on-demand 單檔
（processor.tw_features._fetch_and_land）。

遇到 FinMind IP 封鎖（HTTP 403 ip banned）時：
  - 預設自動等 retry_after 秒後「從中斷處續跑」（不重抓已完成的標的）。
  - 等候超過 --max-wait 秒或封鎖次數超過上限時，乾淨中止並回報剩餘標的。
  - 加 --no-wait 可改為一遇封鎖就立刻中止（給 CI / 想自己排程的情境）。

CLI：python -m data_sources.backfill_tw [days] [--no-wait] [--max-wait SEC]
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta
from typing import Any

import universe
from storage import local_store

from . import finmind_loader
from .finmind_loader import FinMindIPBanned, FinMindQuotaExceeded
from .ingest import TW_MARKET, _dq_filter

logger = logging.getLogger("ai-market-backend.backfill_tw")

# 封鎖時最多自動等候幾次，避免極端情況下無限等下去
_MAX_BAN_WAITS = 5
# 單次封鎖最長願意等多久（秒）；超過就中止，避免一個請求卡住整個 job
_DEFAULT_MAX_WAIT = 1800.0


def backfill_watchlist(
    days: int = 90,
    *,
    wait_on_ban: bool = True,
    max_wait_sec: float = _DEFAULT_MAX_WAIT,
    max_ban_waits: int = _MAX_BAN_WAITS,
) -> dict[str, Any]:
    """回補 watchlist 近 days 日的價格 + 籌碼 + 融資券。

    wait_on_ban: 遇 IP 封鎖時自動等候 retry_after 後續跑（False 則立即中止）。
    max_wait_sec: 單次封鎖最長等候秒數，超過則中止。
    max_ban_waits: 整個 job 最多自動等候幾次，超過則中止。
    """
    end = date.today()
    start = end - timedelta(days=days)
    start_s, end_s = start.isoformat(), end.isoformat()
    symbols = sorted(universe.watchlist_symbols())

    # 攤平成任務清單，用索引推進；遇封鎖時停在同一個 task、等候後重試（不重抓已完成的）。
    tasks = [
        (sym, kind, fetch, write)
        for sym in symbols
        for kind, fetch, write in (
            ("price", finmind_loader.fetch_stock_prices_normalized, local_store.write_prices),
            ("chip", finmind_loader.fetch_stock_chip_normalized, local_store.write_chip),
            ("margin", finmind_loader.fetch_stock_margin_normalized, local_store.write_margin),
        )
    ]

    counts = {"price": [0, 0], "chip": [0, 0], "margin": [0, 0]}  # [ok, fail]
    errors: dict[str, str] = {}
    ban_waits = 0

    def _summary(*, aborted: bool = False, ban: dict[str, Any] | None = None,
                 done_tasks: int = len(tasks)) -> dict[str, Any]:
        remaining = sorted({t[0] for t in tasks[done_tasks:]})
        s: dict[str, Any] = {
            "window": {"start": start_s, "end": end_s, "days": days},
            "symbols": len(symbols),
            "price": {"ok": counts["price"][0], "fail": counts["price"][1]},
            "chip": {"ok": counts["chip"][0], "fail": counts["chip"][1]},
            "margin": {"ok": counts["margin"][0], "fail": counts["margin"][1]},
            "errors": errors,
            "aborted": aborted,
        }
        if aborted:
            s["ban"] = ban
            s["remaining_symbols"] = remaining
        return s

    i = 0
    while i < len(tasks):
        sym, kind, fetch, write = tasks[i]
        try:
            rows = _dq_filter(fetch(sym, start_s, end_s))
            if rows:
                write(rows, TW_MARKET)
                counts[kind][0] += 1
            else:
                counts[kind][1] += 1
                logger.warning("backfill %s %s: 0 valid rows", kind, sym)
        except FinMindIPBanned as ban:
            wait = ban.retry_after
            too_long = wait > max_wait_sec
            if not wait_on_ban or too_long or ban_waits >= max_ban_waits:
                reason = (
                    "已關閉自動等候(--no-wait)" if not wait_on_ban else
                    f"retry_after={wait}s 超過 max_wait={max_wait_sec:.0f}s" if too_long else
                    f"已達自動等候上限 {max_ban_waits} 次"
                )
                logger.error(
                    "FinMind IP 封鎖，中止回補（%s）。完成 %d/%d 任務，剩 %d 檔未補。",
                    reason, i, len(tasks), len({t[0] for t in tasks[i:]}),
                )
                return _summary(
                    aborted=True,
                    ban={"retry_after": wait, "reason": reason, "at_symbol": sym, "at_kind": kind},
                    done_tasks=i,
                )
            ban_waits += 1
            sleep_for = wait + 5  # 多等 5s 緩衝，確保封鎖確實解除
            logger.warning(
                "FinMind IP 封鎖，等 %ds 後從 %s(%s) 續跑（第 %d/%d 次自動等候）",
                sleep_for, sym, kind, ban_waits, max_ban_waits,
            )
            time.sleep(sleep_for)
            continue  # 不推進索引，重試同一個 task
        except FinMindQuotaExceeded:
            # 每小時額度耗盡：等整點回補對 backfill 不划算，直接中止、回報剩餘，稍後續跑。
            logger.error(
                "FinMind 每小時額度耗盡，中止回補。完成 %d/%d 任務，剩 %d 檔未補（稍後再續）。",
                i, len(tasks), len({t[0] for t in tasks[i:]}),
            )
            return _summary(
                aborted=True,
                ban={"reason": "hourly_quota_exceeded", "at_symbol": sym, "at_kind": kind},
                done_tasks=i,
            )
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不阻斷其他
            counts[kind][1] += 1
            errors[f"{kind}:{sym}"] = str(exc)
            logger.warning("backfill %s %s failed: %s", kind, sym, exc)
        i += 1

    summary = _summary()
    logger.info("台股回補完成: %s", summary)
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="台股 watchlist 歷史回補（FinMind per-stock）")
    p.add_argument("days", nargs="?", type=int, default=90, help="回補近幾日（預設 90）")
    p.add_argument("--no-wait", action="store_true",
                   help="遇 FinMind IP 封鎖時立即中止，不自動等候")
    p.add_argument("--max-wait", type=float, default=_DEFAULT_MAX_WAIT,
                   help=f"單次封鎖最長等候秒數，超過則中止（預設 {int(_DEFAULT_MAX_WAIT)}）")
    return p.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    import json
    import sys

    args = _parse_args()
    result = backfill_watchlist(
        args.days,
        wait_on_ban=not args.no_wait,
        max_wait_sec=args.max_wait,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # 被封鎖中止時以非 0 退出，方便外層腳本/排程判斷
    sys.exit(2 if result.get("aborted") else 0)
