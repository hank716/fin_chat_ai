"""全站 AI 成本追蹤 + 上限（M4/M6，對齊 design_docs §25）。

追蹤『全站總花費』（晨報 + 所有人的問答）：每筆 Gemini 呼叫以 token × 模型費率估算 TWD，
累進到 redis 的每日桶與每月桶（含日期，隔日/跨月自然汰換）。互動查詢前先 check_daily_budget。

只有摘要/分析/查詢用 Gemini；資料計算不用（§25.1）。費率為粗估（preview 模型無公開定價），
精確金額以 Google 後台為準。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from redis_client import redis_client

logger = logging.getLogger("ai-market-backend.cost")

# 粗估費率（USD/百萬 token）→ TWD。PRO 比 Flash 貴。
_RATES = {
    "pro": (1.25, 10.0),    # gemini-*-pro：(input, output) USD/M
    "flash": (0.30, 2.50),  # gemini-*-flash
}
_USD_TWD = 32.0


def estimate_cost_twd(input_tokens: int, output_tokens: int, model: str = "") -> float:
    rate_in, rate_out = _RATES["pro"] if "pro" in model.lower() else _RATES["flash"]
    usd = (input_tokens / 1e6) * rate_in + (output_tokens / 1e6) * rate_out
    return round(usd * _USD_TWD, 4)


def _now():
    return datetime.now(ZoneInfo(settings.schedule_tz))


def _day_key() -> str:
    return f"cost:day:{_now():%Y%m%d}"


def _month_key() -> str:
    return f"cost:month:{_now():%Y%m}"


def current_month() -> str:
    return _now().strftime("%Y-%m")


def _get(key: str) -> float:
    try:
        v = redis_client.get(key)
        return round(float(v), 4) if v else 0.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取成本 %s 失敗: %s", key, exc)
        return 0.0


def today_total() -> float:
    return _get(_day_key())


def month_total() -> float:
    return _get(_month_key())


def record_cost(cost_twd: float) -> None:
    """把一筆花費累加到當日與當月全站桶（日桶 48h、月桶 70d TTL）。"""
    try:
        dk, mk = _day_key(), _month_key()
        redis_client.incrbyfloat(dk, cost_twd)
        redis_client.expire(dk, 48 * 3600)
        redis_client.incrbyfloat(mk, cost_twd)
        redis_client.expire(mk, 70 * 24 * 3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning("記錄成本失敗: %s", exc)


def check_budget() -> tuple[bool, str, float, float]:
    """檢查每日與每月全站上限。回 (是否仍有額度, 婉拒原因, 今日已花, 每日上限)。"""
    d, m = today_total(), month_total()
    dl = float(settings.daily_cost_limit_twd)
    ml = float(settings.monthly_cost_limit_twd)
    if m >= ml:
        return False, f"本月全站 AI 額度已用完（NT${m:.1f} / NT${ml:.0f}）", d, dl
    if d >= dl:
        return False, f"今日全站 AI 額度已用完（NT${d:.1f} / NT${dl:.0f}，含晨報與問答）", d, dl
    return True, "", d, dl
