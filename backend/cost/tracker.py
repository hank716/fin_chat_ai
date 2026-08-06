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
    # Flash-Lite（意圖分類用）：對應 flash-lite-latest →Gemini 3.1 Flash-Lite（與 pro/flash 同屬 3.x
    # 別名世代）。官方定價（標準層、text）input $0.25 / output $1.50 / cache $0.025，不分級距
    # （https://ai.google.dev/gemini-api/docs/pricing，2026-06-03 核對）。
    "flash-lite": {
        "small": (0.25, 1.50, 0.025),
        "large": (0.25, 1.50, 0.025),
    },
}
_TIER_THRESHOLD = 200_000               # prompt tokens 超過此值套用 large 級距
# Google 搜尋 grounding：每月前 5,000 次免費（Gemini 3 共用），之後 $14 / 1,000 次（每請求計價）。
_GROUNDING_FREE_PER_MONTH = 5000
_GROUNDING_USD_PER_REQUEST = 14.0 / 1000
_USD_TWD = 32.0                         # 固定估算匯率；TWD 大幅波動時再調整

# ─────────────────────────────────────────────────────────────────────────
# 決策層（Anthropic）費率（spec 022）。USD / 百萬 token，欄位＝(input, output, cached_read)。
# 不分級距。⚠️ 這張表若沒加，`claude-opus-5` 會落到下面的 flash 分支（$1.50/$9.00）而
# **低估約 3 倍**——而 check_budget() 正是靠這個數字擋 /ask，等於預算閘門靜默失效。
# ─────────────────────────────────────────────────────────────────────────
_PRICING_ANTHROPIC = {
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (3.00, 15.00, 0.30),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
_ANTHROPIC_DEFAULT = _PRICING_ANTHROPIC["claude-opus-5"]   # 未知 claude-* 一律以最貴檔估（寧可高估）
# 快取寫入 premium（倍率，乘在 input 價上）：5 分鐘 1.25×、1 小時 2×。
_CACHE_WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}
# Anthropic web_search：按次計價。⚠️ 這個數字**必須**對照 Anthropic 定價頁核實後再改，
# 不要憑印象填；估錯會讓查證成本失真（web_fetch 不另計費，其內容以 input token 計）。
_WEB_SEARCH_USD_PER_REQUEST = 10.0 / 1000


def _is_anthropic(model: str) -> bool:
    return model.lower().startswith("claude-")


def _rates(model: str, prompt_tokens: int) -> tuple[float, float, float]:
    m = model.lower()
    # 決策層先判：`claude-opus-5` 不含 lite/pro/flash 任何字串，落到下面會被當 flash 算。
    if _is_anthropic(m):
        return _PRICING_ANTHROPIC.get(m, _ANTHROPIC_DEFAULT)
    # 先判 lite（"flash-lite" 也含 "flash"，順序不可顛倒），再判 pro，最後才 flash。
    if "lite" in m:
        fam = "flash-lite"
    elif "pro" in m:
        fam = "pro"
    else:
        fam = "flash"
    tier = "large" if prompt_tokens > _TIER_THRESHOLD else "small"
    return _PRICING[fam][tier]


def estimate_cost_twd(
    input_tokens: int,
    output_tokens: int,
    model: str = "",
    *,
    cached_tokens: int = 0,
    tool_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_ttl: str = "5m",
) -> float:
    """單次呼叫的 token 費用（TWD）。

    ⚠️ **兩家供應商的 input_tokens 語意相反**，這裡依 model 分流，不可共用同一套減法：
    - Gemini：`promptTokenCount` **已含** cached → 要減掉 cached 才是未命中的部分。
    - Anthropic：`input_tokens` **不含** cache（cache_read/cache_creation 各自獨立回報）
      → 直接相減會把快取部分重複扣掉，低估成本。

    cache_write_tokens（Anthropic `cache_creation_input_tokens`）以 input 價 × premium 計：
    5m 為 1.25×、1h 為 2×。Gemini 的明確快取是按存放時間計費的另一套模型，不走這裡。
    """
    rate_in, rate_out, rate_cached = _rates(model, input_tokens)
    if _is_anthropic(model):
        billable_input = max(input_tokens, 0) + max(tool_tokens, 0)
    else:
        billable_input = max(input_tokens - cached_tokens, 0) + max(tool_tokens, 0)
    write_multiplier = _CACHE_WRITE_MULTIPLIER.get(cache_ttl, 1.25)
    usd = (
        billable_input / 1e6 * rate_in
        + max(cached_tokens, 0) / 1e6 * rate_cached
        + max(cache_write_tokens, 0) / 1e6 * rate_in * write_multiplier
        + output_tokens / 1e6 * rate_out
    )
    return round(usd * _USD_TWD, 6)


def cost_of_usage(usage: dict, model: str, *, grounded: bool = False) -> float:
    """把單次 LLM 回傳的 usage（各 client 的 `_usage_of` 整理過）換算成 TWD。

    這就是「Token 計數器中間件」：唯一將 token 用量 → 金額 的入口，含 cache 折扣/premium、
    Gemini >200k 級距、Google grounding、Anthropic web_search 按次計費。

    grounded=True（Gemini 用到 Google 搜尋）時加上當次 grounding 邊際費用（超免費額才計）。
    決策層的連網查證則走 usage 內的 `web_search_requests`，兩者計價方式不同、不可混用。
    """
    cost = estimate_cost_twd(
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        model=model,
        cached_tokens=int(usage.get("cached_tokens", 0)),
        tool_tokens=int(usage.get("tool_tokens", 0)),
        cache_write_tokens=int(usage.get("cache_write_tokens", 0)),
        cache_ttl=str(usage.get("cache_ttl", "5m")),
    )
    if grounded:
        cost += record_grounding_request()
    searches = int(usage.get("web_search_requests", 0))
    if searches > 0:
        cost += round(searches * _WEB_SEARCH_USD_PER_REQUEST * _USD_TWD, 6)
    return round(cost, 6)


def _now():
    return datetime.now(ZoneInfo(settings.schedule_tz))


def _day_key() -> str:
    return f"cost:day:{_now():%Y%m%d}"


def _month_key() -> str:
    return f"cost:month:{_now():%Y%m}"


def _grounding_key() -> str:
    return f"cost:grounding:{_now():%Y%m}"


# spec 022：分層後合計金額看不出 Gemini（廣度召回）與 Anthropic（決策查證）各佔多少，
# 而那正是換供應商後最需要盯的數字。彙總桶維持不變（預算閘門仍看它），另開 per-provider 桶。
PROVIDERS = ("gemini", "anthropic")


def provider_of(model: str) -> str:
    """由 model 名稱判供應商。與 `_rates()` 用同一個 `claude-` 前綴判準，兩者不可分歧。"""
    return "anthropic" if _is_anthropic(model) else "gemini"


def _provider_month_key(provider: str) -> str:
    return f"cost:month:{_now():%Y%m}:{provider}"


def month_by_provider() -> dict[str, float]:
    """本月各供應商累計（TWD）。桶不存在＝0，不影響首頁渲染。"""
    return {p: _get(_provider_month_key(p)) for p in PROVIDERS}


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


def record_cost(cost_twd: float, provider: str | None = None) -> None:
    """把一筆花費累加到當日與當月全站桶（日桶 48h、月桶 70d TTL）。

    `provider`（"gemini" / "anthropic"）另外累進 per-provider 月桶，供首頁拆分顯示。
    **彙總桶一定會寫**——預算閘門看的是它，不能因為漏帶 provider 就少計。
    """
    try:
        dk, mk = _day_key(), _month_key()
        redis_client.incrbyfloat(dk, cost_twd)
        redis_client.expire(dk, 48 * 3600)
        redis_client.incrbyfloat(mk, cost_twd)
        redis_client.expire(mk, 70 * 24 * 3600)
        if provider in PROVIDERS:
            pk = _provider_month_key(provider)
            redis_client.incrbyfloat(pk, cost_twd)
            redis_client.expire(pk, 70 * 24 * 3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning("記錄成本失敗: %s", exc)


def set_month_total(total_twd: float) -> float:
    """把『當月全站累計』校準成 Google 後台的實際金額（覆寫月桶，保留/補回 TTL）。

    估算與後台必有落差（cache 折扣、級距、grounding、匯率、latest 別名版本）；定期以後台數字
    重設基準，之後照常累加即可讓首頁橫幅貼近真實。回傳設定後的月度值。
    """
    try:
        mk = _month_key()
        redis_client.set(mk, round(float(total_twd), 4), keepttl=True)
        if redis_client.ttl(mk) < 0:          # 原本無 TTL（或新建）→ 補回月桶 70d
            redis_client.expire(mk, 70 * 24 * 3600)
        logger.info("校準當月全站成本為 NT$%.4f", total_twd)
        return month_total()
    except Exception as exc:  # noqa: BLE001
        logger.warning("校準月度成本失敗: %s", exc)
        return month_total()


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
