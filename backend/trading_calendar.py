"""台股交易日判斷（M7 相關）。

晨報只在「台股有開盤的日子」產生/推送。無公開 FinMind 日曆 dataset，故用：
  週末（六日）一律非交易日 + configs/tw_holidays.json 的國定假日清單（可校正）。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("ai-market-backend.calendar")

HOLIDAYS_PATH = Path(__file__).resolve().parent / "configs" / "tw_holidays.json"


@lru_cache(maxsize=1)
def _holidays() -> frozenset[str]:
    try:
        data = json.loads(HOLIDAYS_PATH.read_text(encoding="utf-8"))
        return frozenset(data.get("dates", []))
    except Exception as exc:  # noqa: BLE001 — 缺檔只靠週末判斷
        logger.warning("台股休市日設定讀取失敗 (%s): %s", HOLIDAYS_PATH, exc)
        return frozenset()


def is_tw_trading_day(d: date) -> bool:
    """週一~週五且非休市日 → 交易日。"""
    if d.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    return d.isoformat() not in _holidays()
