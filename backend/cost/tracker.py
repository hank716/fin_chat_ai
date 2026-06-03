"""全站 AI 成本追蹤 + 上限（M4/M6，對齊 design_docs §25）。

追蹤『全站總花費』（晨報 + 所有人的問答）：每筆 Gemini 呼叫以 token × 模型費率估算 TWD，
累進到 redis 的每日桶與每月桶（含日期，隔日/跨月自然汰換）。互動查詢前先 check_budget。

費率對齊 Google 官方定價 https://ai.google.dev/gemini-api/docs/pricing：
  - 精確費用＝ **未命中 input×input 價 + 命中 cache×cache 價 + output×output 價**，
    而非 totalTokens×單一價（後者必失準）。output 含 thinking tokens。
  - Gemini 3.1 Pro 有 **>200k tokens 級距**（input/output/cache 全部加倍）。
  - **Google 搜尋 grounding** 每月前 5,000 次免費（Gemini 3 共用），超過後 $14 / 1,000 次。
仍為估算（latest 別名實際指向哪個版本、匯率會變動），精確金額以 Google 後台為準，
但已涵蓋造成後台落差的三大來源（cache 折扣、>200k 級距、grounding）。

⚠️ 本檔費率對應 `*-latest` 別名「當前」指向的版本（pro-latest→Gemini 3.1 Pro、
flash-latest→Gemini 3.5 Flash，於 2026-06-03 核對）。Google 把別名重新指向新版時，
這張表必須同步更新，否則會再次低估。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from redis_client import redis_client

logger = logging.getLogger("ai-market-backend.cost")

# 官方定價（USD / 百萬 token）。每個模型分 prompt「<=200k」(small) 與「>200k」(large) 兩級距，
# 欄位＝(input, output, cached_input)。Flash 無級距，兩級距相同。
# 對應 *-latest 當前版本：pro→Gemini 3.1 Pro、flash→Gemini 3.5 Flash（2026-06-03 核對）。
_PRICING = {
    "pro": {
        "small": (2.00, 12.0, 0.20),    # prompt <= 200k tokens
        "large": (4.00, 18.0, 0.40),    # prompt >  200k tokens
    },
    "flash": {
        "small": (1.50, 9.00, 0.15),    # 3.5 Flash 不分級距
        "large": (1.50, 9.00, 0.15),
    },
}
_TIER_THRESHOLD = 200_000               # prompt tokens 超過此值套用 large 級距
# Google 搜尋 grounding：每月前 5,000 次免費（Gemini 3 共用），之後 $14 / 1,000 次（每請求計價）。
_GROUNDING_FREE_PER_MONTH = 5000
_GROUNDING_USD_PER_REQUEST = 14.0 / 1000
_USD_TWD = 32.0                         # 固定估算匯率；TWD 大幅波動時再調整


def _rates(model: str, prompt_tokens: int) -> tuple[float, float, float]:
    fam = "pro" if "pro" in model.lower() else "flash"
    tier = "large" if prompt_tokens > _TIER_THRESHOLD else "small"
    return _PRICING[fam][tier]


def estimate_cost_twd(
    input_tokens: int,
    output_tokens: int,
    model: str = "",
    *,
    cached_tokens: int = 0,
    tool_tokens: int = 0,
) -> float:
    """單次呼叫的 token 費用（TWD）。

    input_tokens＝promptTokenCount（**已含** cached），cached_tokens＝其中命中快取的部分。
    級距依 prompt 總量判定；命中快取部分以 cache 價、其餘 input 以 input 價、output 以 output 價。
    tool_tokens（toolUsePromptTokenCount）與 promptTokenCount 分開回傳，按 input 價計。
    """
    rate_in, rate_out, rate_cached = _rates(model, input_tokens)
    billable_input = max(input_tokens - cached_tokens, 0) + max(tool_tokens, 0)
    usd = (
        billable_input / 1e6 * rate_in
        + max(cached_tokens, 0) / 1e6 * rate_cached
        + output_tokens / 1e6 * rate_out
    )
    return round(usd * _USD_TWD, 6)


def cost_of_usage(usage: dict, model: str, *, grounded: bool = False) -> float:
    """把單次 Gemini 回傳的 usageMetadata（_usage_of 整理過）換算成 TWD。

    這就是「Token 計數器中間件」：唯一將 token 用量 → 金額 的入口，含 cache 折扣、>200k 級距、
    grounding。grounded=True（用到 Google 搜尋）時，加上當次 grounding 的邊際費用（超免費額才計）。
    """
    cost = estimate_cost_twd(
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        model=model,
        cached_tokens=int(usage.get("cached_tokens", 0)),
        tool_tokens=int(usage.get("tool_tokens", 0)),
    )
    if grounded:
        cost += record_grounding_request()
    return round(cost, 6)


def _now():
    return datetime.now(ZoneInfo(settings.schedule_tz))


def _day_key() -> str:
    return f"cost:day:{_now():%Y%m%d}"


def _month_key() -> str:
    return f"cost:month:{_now():%Y%m}"


def _grounding_key() -> str:
    return f"cost:grounding:{_now():%Y%m}"


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


def record_grounding_request() -> float:
    """記一次 Google 搜尋 grounding（全站當月計數）。回傳這次的邊際費用 TWD（前 5,000 次/月免費）。"""
    try:
        gk = _grounding_key()
        n = int(redis_client.incr(gk))
        redis_client.expire(gk, 70 * 24 * 3600)
        if n > _GROUNDING_FREE_PER_MONTH:
            return round(_GROUNDING_USD_PER_REQUEST * _USD_TWD, 6)
        return 0.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("記錄 grounding 失敗: %s", exc)
        return 0.0


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
