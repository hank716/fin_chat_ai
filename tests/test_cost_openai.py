"""OpenAI 計價（spec 026 US3）。

「OpenAI 省 55%」的結論完全仰賴這裡算對。OpenAI 的 input 語意與另外兩家都不同：
`input_tokens` 是**總數**，其中 cached read 與 cache write 各自獨立計費、三者互斥。
算錯（重複扣或少算）會讓 A/B 結論與預算閘門同時失效。
"""
from __future__ import annotations

import logging

import pytest

from cost import tracker as ct


def _twd(usd: float) -> float:
    return usd * 32.0


def test_spec_us3_scenario_1_hand_calculated():
    """spec US3 情境 1：input 60k（cached 40k）、output 18k（其中 reasoning 10k）、搜尋 2 次。"""
    usage = {
        "input_tokens": 60_000,
        "cached_tokens": 40_000,
        "cache_write_tokens": 0,
        "output_tokens": 18_000,
        "reasoning_tokens": 10_000,     # 已含在 output_tokens 內，不得另計
        "web_search_requests": 2,
    }
    expected_usd = (
        20_000 / 1e6 * 2.00          # 未命中 = 60k - 40k
        + 40_000 / 1e6 * 0.10        # cached read
        + 18_000 / 1e6 * 10.00       # output（含 reasoning）
        + 2 * 0.01                   # web_search $10 / 1k 次
    )
    assert ct.cost_of_usage(usage, "gpt-6.1-sol") == pytest.approx(_twd(expected_usd), abs=1e-6)


def test_cached_and_cache_write_are_mutually_exclusive():
    """三種 input 費率互斥：未命中 = input − cached − write，write 不可再被當未命中計一次。"""
    usage = {"input_tokens": 60_000, "cached_tokens": 30_000,
             "cache_write_tokens": 20_000, "output_tokens": 0}
    expected_usd = 10_000 / 1e6 * 2.00 + 30_000 / 1e6 * 0.10 + 20_000 / 1e6 * 2.50
    assert ct.cost_of_usage(usage, "gpt-6.1-sol") == pytest.approx(_twd(expected_usd), abs=1e-6)


def test_cache_ttl_does_not_apply_anthropic_multiplier():
    """OpenAI 的 cache write 有自己的牌價，不能套 Anthropic 的 1.25× / 2× 倍率。"""
    usage = {"input_tokens": 10_000, "cache_write_tokens": 10_000,
             "output_tokens": 0, "cache_ttl": "1h"}
    assert ct.cost_of_usage(usage, "gpt-6.1-sol") == pytest.approx(
        _twd(10_000 / 1e6 * 2.50), abs=1e-6)


def test_long_context_tier_above_272k():
    """>272k input 套長文級距，避免日後 features 變大時靜默低估。"""
    small = ct.cost_of_usage({"input_tokens": 272_000, "output_tokens": 0}, "gpt-6.1-sol")
    large = ct.cost_of_usage({"input_tokens": 300_000, "output_tokens": 0}, "gpt-6.1-sol")
    assert small == pytest.approx(_twd(272_000 / 1e6 * 2.00), abs=1e-6)
    assert large > _twd(300_000 / 1e6 * 2.00)


def test_unknown_gpt_model_priced_at_most_expensive_with_warning(caplog):
    """寧可高估：未知 gpt-* 以 gpt-6-astra 估並警告（同 Anthropic 未知 claude-* 的處理）。"""
    usage = {"input_tokens": 200_000, "output_tokens": 0}
    with caplog.at_level(logging.WARNING, logger="ai-market-backend.cost"):
        cost = ct.cost_of_usage(usage, "gpt-7-nova")
    assert cost == pytest.approx(_twd(200_000 / 1e6 * 10.00), abs=1e-6)
    assert any("gpt-7-nova" in r.getMessage() for r in caplog.records)


def test_reasoning_tokens_not_billed_separately():
    base = {"input_tokens": 0, "output_tokens": 5_000}
    with_reasoning = {**base, "reasoning_tokens": 4_000}
    assert ct.cost_of_usage(base, "gpt-6.1-sol") == ct.cost_of_usage(with_reasoning, "gpt-6.1-sol")


@pytest.mark.parametrize("model,provider", [
    ("gpt-6.1-sol", "openai"),
    ("gpt-5.6-terra", "openai"),
    ("o4-mini", "openai"),
    ("claude-opus-5", "anthropic"),
    ("gemini-flash-latest", "gemini"),
    ("gemini-pro-latest", "gemini"),
])
def test_provider_of(model, provider):
    assert ct.provider_of(model) == provider


def test_openai_does_not_fall_into_gemini_flash_pricing():
    """與 claude-* 同一個坑：沒有 OpenAI 分支時 gpt-* 會落到 flash 費率而嚴重低估。"""
    usage = {"input_tokens": 0, "output_tokens": 1_000_000}
    assert ct.cost_of_usage(usage, "gpt-6.1-sol") == pytest.approx(_twd(10.00), abs=1e-6)


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, float] = {}

    def incrbyfloat(self, key, amount):
        self.store[key] = self.store.get(key, 0.0) + float(amount)
        return self.store[key]

    def get(self, key):
        return None if key not in self.store else str(self.store[key])

    def expire(self, key, ttl):  # noqa: ARG002
        return True


def test_openai_bucket_recorded_and_counted_in_aggregate(monkeypatch):
    """openai 桶獨立累計，且彙總桶照樣加——預算閘門（日／月上限、節儉模式）看的是彙總。"""
    monkeypatch.setattr(ct, "redis_client", _FakeRedis())
    ct.record_cost(12.0, provider="openai")
    ct.record_cost(26.0, provider="anthropic")
    by = ct.month_by_provider()
    assert by["openai"] == pytest.approx(12.0)
    assert by["anthropic"] == pytest.approx(26.0)
    assert ct.month_total() == pytest.approx(38.0)
    assert ct.today_total() == pytest.approx(38.0)
