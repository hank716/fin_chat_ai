"""WP3.5：成本計價數學（對照 Google 官方定價手算）。純函式，不碰 redis。"""
from __future__ import annotations

import pytest

from cost import tracker as ct

USD_TWD = 32.0


def test_pro_small_tier_basic():
    # pro small: input $2/1M, output $12/1M；input 100k、output 1k
    twd = ct.estimate_cost_twd(100_000, 1_000, "gemini-pro-latest")
    assert twd == pytest.approx((100_000 / 1e6 * 2.0 + 1_000 / 1e6 * 12.0) * USD_TWD)   # 6.784


def test_cache_discount_applied():
    # 100k input 內含 40k 命中快取：60k 走 input 價、40k 走 cache 價(0.20)
    twd = ct.estimate_cost_twd(100_000, 1_000, "gemini-pro-latest", cached_tokens=40_000)
    expect = (60_000 / 1e6 * 2.0 + 40_000 / 1e6 * 0.20 + 1_000 / 1e6 * 12.0) * USD_TWD
    assert twd == pytest.approx(expect)                                                 # 4.48


def test_large_tier_over_200k():
    # >200k 套 large 級距：input $4、output $18
    twd = ct.estimate_cost_twd(250_000, 1_000, "gemini-pro-latest")
    assert twd == pytest.approx((250_000 / 1e6 * 4.0 + 1_000 / 1e6 * 18.0) * USD_TWD)   # 32.576


def test_tool_tokens_billed_as_input():
    twd = ct.estimate_cost_twd(1_000, 100, "gemini-pro-latest", tool_tokens=500)
    expect = ((1_000 + 500) / 1e6 * 2.0 + 100 / 1e6 * 12.0) * USD_TWD
    assert twd == pytest.approx(expect)                                                 # 0.1344


def test_rates_model_routing_and_tier_boundary():
    assert ct._rates("gemini-flash-lite-latest", 1_000) == (0.25, 1.50, 0.025)   # lite 先於 flash
    assert ct._rates("gemini-pro-latest", 1_000) == (2.00, 12.0, 0.20)
    assert ct._rates("gemini-flash-latest", 1_000) == (1.50, 9.00, 0.15)
    assert ct._rates("gemini-pro-latest", 200_000) == (2.00, 12.0, 0.20)         # 邊界=small（>200k 才 large）
    assert ct._rates("gemini-pro-latest", 200_001) == (4.00, 18.0, 0.40)


def test_flash_lite_pricing():
    twd = ct.estimate_cost_twd(100_000, 1_000, "gemini-flash-lite-latest")
    assert twd == pytest.approx((100_000 / 1e6 * 0.25 + 1_000 / 1e6 * 1.50) * USD_TWD)  # 0.848


def test_cost_of_usage_maps_usage_fields():
    usage = {"input_tokens": 100_000, "output_tokens": 1_000, "cached_tokens": 0, "tool_tokens": 0}
    twd = ct.cost_of_usage(usage, "gemini-pro-latest", grounded=False)
    assert twd == pytest.approx(ct.estimate_cost_twd(100_000, 1_000, "gemini-pro-latest"))


# ─────────────────────────────────────────────────────────────────────────
# spec 022：決策層（Anthropic）計價。
# ─────────────────────────────────────────────────────────────────────────


def test_claude_does_not_fall_through_to_flash_rates():
    """回歸測試：`claude-opus-5` 不含 lite/pro/flash 任一字串。

    若 `_rates()` 沒有先判 `claude-` 前綴，它會落到 else 分支被當成 flash（$1.50/$9.00），
    **低估約 3 倍**——而 check_budget() 正是靠這個數字擋 /ask，等於預算閘門靜默失效。
    這條測試就是釘住那個破口。
    """
    assert ct._rates("claude-opus-5", 1_000) == (5.00, 25.00, 0.50)
    assert ct._rates("claude-opus-5", 1_000) != ct._PRICING["flash"]["small"]


def test_claude_rates_by_model():
    assert ct._rates("claude-sonnet-5", 1_000) == (3.00, 15.00, 0.30)
    assert ct._rates("claude-haiku-4-5", 1_000) == (1.00, 5.00, 0.10)


def test_unknown_claude_model_estimates_high():
    """未知的 claude-* 以最貴檔估——寧可高估也不要讓預算閘門低估而失守。"""
    assert ct._rates("claude-something-new", 1_000) == ct._ANTHROPIC_DEFAULT


def test_claude_has_no_200k_tier():
    """Gemini pro 有 >200k 級距，Anthropic 沒有——不可套用同一套級距邏輯。"""
    assert ct._rates("claude-opus-5", 1_000) == ct._rates("claude-opus-5", 500_000)


def test_anthropic_input_tokens_exclude_cache():
    """⚠️ 兩家語意相反，這是最容易寫錯的一條。

    Gemini 的 promptTokenCount **已含** cached → 要減。
    Anthropic 的 input_tokens **不含** cache → 再減就重複扣、低估成本。
    """
    twd = ct.estimate_cost_twd(
        100_000, 1_000, "claude-opus-5", cached_tokens=40_000,
    )
    # 100k 全額 input + 40k cache read（不是 60k input）
    expect = (100_000 / 1e6 * 5.00 + 40_000 / 1e6 * 0.50 + 1_000 / 1e6 * 25.00) * USD_TWD
    assert twd == pytest.approx(expect)


def test_gemini_input_tokens_include_cache_unchanged():
    """對照組：Gemini 側的減法行為不能被上面的改動波及。"""
    twd = ct.estimate_cost_twd(100_000, 1_000, "gemini-pro-latest", cached_tokens=40_000)
    expect = (60_000 / 1e6 * 2.0 + 40_000 / 1e6 * 0.20 + 1_000 / 1e6 * 12.0) * USD_TWD
    assert twd == pytest.approx(expect)


@pytest.mark.parametrize("ttl,multiplier", [("5m", 1.25), ("1h", 2.0)])
def test_cache_write_premium(ttl, multiplier):
    """寫入快取是 input 價 × premium。這正是「稀疏使用時開快取反而更貴」的來源。"""
    twd = ct.estimate_cost_twd(
        1_000, 0, "claude-opus-5", cache_write_tokens=100_000, cache_ttl=ttl,
    )
    expect = (1_000 / 1e6 * 5.00 + 100_000 / 1e6 * 5.00 * multiplier) * USD_TWD
    assert twd == pytest.approx(expect)


def test_cache_write_never_cheaper_than_plain_input():
    """健全性：寫快取一定比不寫貴（1.25×/2×），否則 premium 方向寫反了。"""
    plain = ct.estimate_cost_twd(100_000, 0, "claude-opus-5")
    written = ct.estimate_cost_twd(0, 0, "claude-opus-5",
                                   cache_write_tokens=100_000, cache_ttl="5m")
    assert written > plain


def test_web_search_billed_per_request():
    """web_search 按次計價；web_fetch 不另計費（其內容以 input token 計）。"""
    base = ct.cost_of_usage({"input_tokens": 1_000, "output_tokens": 100}, "claude-opus-5")
    with_search = ct.cost_of_usage(
        {"input_tokens": 1_000, "output_tokens": 100, "web_search_requests": 5},
        "claude-opus-5",
    )
    expect = base + round(5 * ct._WEB_SEARCH_USD_PER_REQUEST * USD_TWD, 6)
    assert with_search == pytest.approx(expect)


def test_google_grounding_not_charged_on_claude_path():
    """Google grounding 邊際費用只屬召回層；決策層走 web_search_requests，兩者不可混算。"""
    usage = {"input_tokens": 1_000, "output_tokens": 100}
    assert ct.cost_of_usage(usage, "claude-opus-5", grounded=False) == pytest.approx(
        ct.estimate_cost_twd(1_000, 100, "claude-opus-5")
    )
