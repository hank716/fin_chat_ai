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

# 粗估費率（USD/百萬 token）→ TWD。家用估算用，寧可高估。PRO 比 Flash 貴。
_RATES = {
    "pro": (1.25, 10.0),    # gemini-*-pro：(input, output) USD/M
    "flash": (0.30, 2.50),  # gemini-*-flash
}
_USD_TWD = 32.0


def estimate_cost_twd(input_tokens: int, output_tokens: int, model: str = "") -> float:
    rate_in, rate_out = _RATES["pro"] if "pro" in model.lower() else _RATES["flash"]
    usd = (input_tokens / 1e6) * rate_in + (output_tokens / 1e6) * rate_out
    return round(usd * _USD_TWD, 4)


SYSTEM_USER = "system"  # 每日晨報等系統級 Gemini 花費歸戶


def _now():
    return datetime.now(ZoneInfo(settings.schedule_tz))


def _key(user_id: str) -> str:
    return f"cost:{_now():%Y%m%d}:{user_id}"


def _month_key(user_id: str | None = None) -> str:
    suffix = user_id or "all"
    return f"cost:m:{_now():%Y%m}:{suffix}"


def current_month() -> str:
    return _now().strftime("%Y-%m")


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
    """累加成本到當日(per-user)與當月(per-user + all)桶，回傳當日該 user 累計。

    當日桶 48h TTL（隔日失效）；當月桶 ~70 天 TTL（跨月自然汰換）。
    """
    try:
        dk = _key(user_id)
        total = redis_client.incrbyfloat(dk, cost_twd)
        redis_client.expire(dk, 48 * 3600)
        for mk in (_month_key(user_id), _month_key()):  # per-user + all
            redis_client.incrbyfloat(mk, cost_twd)
            redis_client.expire(mk, 70 * 24 * 3600)
        return round(float(total), 4)
    except Exception as exc:  # noqa: BLE001
        logger.warning("記錄成本失敗: %s", exc)
        return cost_twd


def month_total(user_id: str | None = None) -> float:
    """本月累計花費（user_id 省略=全系統含晨報與所有使用者查詢）。"""
    try:
        v = redis_client.get(_month_key(user_id))
        return round(float(v), 2) if v else 0.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取本月成本失敗: %s", exc)
        return 0.0
