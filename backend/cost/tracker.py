"""每位使用者每日 AI 成本追蹤 + 上限（M4，對齊 design_docs §25）。

Gemini 計費以 token 估算 TWD，累進到 redis（key 含日期，隔日自然歸零）。
互動查詢前先 check_budget；查詢後 record_cost。每日上限走 env DAILY_COST_LIMIT_TWD。

只有「摘要/分析/查詢」用 Gemini；資料計算不用（§25.1）。每日晨報屬系統成本，
這裡只追蹤 Discord 互動的 per-user 查詢成本。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from redis_client import redis_client

logger = logging.getLogger("ai-market-backend.cost")

# gemini-flash 粗估費率（USD/百萬 token）→ TWD。家用估算用，寧可高估。
_USD_PER_M_INPUT = 0.30
_USD_PER_M_OUTPUT = 2.50
_USD_TWD = 32.0


def estimate_cost_twd(input_tokens: int, output_tokens: int) -> float:
    usd = (input_tokens / 1e6) * _USD_PER_M_INPUT + (output_tokens / 1e6) * _USD_PER_M_OUTPUT
    return round(usd * _USD_TWD, 4)


def _key(user_id: str) -> str:
    today = datetime.now(ZoneInfo(settings.schedule_tz)).strftime("%Y%m%d")
    return f"cost:{today}:{user_id}"


def today_spent(user_id: str) -> float:
    try:
        v = redis_client.get(_key(user_id))
        return float(v) if v else 0.0
    except Exception as exc:  # noqa: BLE001 — redis 掛掉不阻斷查詢，視為 0
        logger.warning("讀取成本失敗，視為 0: %s", exc)
        return 0.0


def check_budget(user_id: str) -> tuple[bool, float, float]:
    """回 (是否仍有額度, 今日已花, 上限)。"""
    limit = float(settings.daily_cost_limit_twd)
    spent = today_spent(user_id)
    return spent < limit, spent, limit


def record_cost(user_id: str, cost_twd: float) -> float:
    """累加成本，回傳累計值。redis key 設 48h TTL（隔日自然失效）。"""
    try:
        k = _key(user_id)
        total = redis_client.incrbyfloat(k, cost_twd)
        redis_client.expire(k, 48 * 3600)
        return round(float(total), 4)
    except Exception as exc:  # noqa: BLE001
        logger.warning("記錄成本失敗: %s", exc)
        return cost_twd
