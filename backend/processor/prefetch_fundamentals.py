"""預抓基本面到磁碟快取（可選，對齊「用時間換運算資源」的設計）。

機器有開著時（或遠端喚醒後）先把 watchlist 的月營收/季財報暖進磁碟快取，之後的晨報大多
直接讀磁碟、幾乎不必再打 FinMind，避免 08:30 一次 ~225 calls 的尖峰與 rate limit。
落地與 TTL 邏輯都在 fundamentals._cached；這裡只是「批次跑一遍 build_fundamentals」。

範圍預設＝精選 watchlist（configs/universe/tw.json 的 sectors，約 48 檔）。
CLI： python -m processor.prefetch_fundamentals [--force] [limit]
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from processor.fundamentals import build_fundamentals

logger = logging.getLogger("ai-market-backend.prefetch")

_WATCHLIST_JSON = Path(__file__).resolve().parent.parent / "configs" / "universe" / "tw.json"


def watchlist_symbols() -> list[str]:
    """精選 watchlist 代號（tw.json 的 sectors 攤平去重）。讀不到回空清單。"""
    try:
        data = json.loads(_WATCHLIST_JSON.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取 watchlist %s 失敗: %s", _WATCHLIST_JSON, exc)
        return []
    return sorted({s for lst in data.get("sectors", {}).values() for s in lst})


def prefetch(symbols: list[str] | None = None, *, force: bool = False) -> dict[str, Any]:
    """逐檔跑 build_fundamentals（會落地磁碟）。回 {total, ok, empty, error, elapsed_sec}。

    FinMind 節流由 rate_limiter 自動處理（finmind 0.5 req/s）；本函式刻意循序、不並發，
    讓速率平緩。force=True 則無視 TTL 全部重抓。
    """
    syms = symbols if symbols is not None else watchlist_symbols()
    t0 = time.monotonic()
    ok = empty = error = 0
    for sym in syms:
        try:
            fu = build_fundamentals(sym, force=force)
            if fu:
                ok += 1
            else:
                empty += 1
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不中斷整批
            error += 1
            logger.warning("預抓基本面 %s 失敗: %s", sym, exc)
    elapsed = round(time.monotonic() - t0, 1)
    result = {"total": len(syms), "ok": ok, "empty": empty, "error": error,
              "elapsed_sec": elapsed, "force": force}
    logger.info("基本面預抓完成: %s", result)
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    limit = int(args[0]) if args and args[0].isdigit() else None
    syms = watchlist_symbols()
    if limit:
        syms = syms[:limit]
    print(json.dumps(prefetch(syms, force=force), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
