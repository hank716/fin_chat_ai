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
