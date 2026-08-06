"""成本按供應商拆分（spec 022）。

分層後只看合計金額，看不出 Gemini 廣度召回與 Claude 決策查證各佔多少——
而那正是換供應商後最需要盯的數字。彙總桶維持不變（預算閘門仍看它），另開 per-provider 桶。
"""
from __future__ import annotations

import pytest

from cost import tracker as ct


class _FakeRedis:
    """只實作 tracker 用到的幾個操作，避免測試依賴真的 redis。"""

    def __init__(self):
        self.store: dict[str, float] = {}

    def incrbyfloat(self, key, amount):
        self.store[key] = self.store.get(key, 0.0) + float(amount)
        return self.store[key]

    def get(self, key):
        return None if key not in self.store else str(self.store[key])

    def expire(self, key, ttl):  # noqa: ARG002
        return True


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(ct, "redis_client", r)
    return r


def test_provider_of_matches_rates_prefix_rule():
    """與 _rates() 用同一個 claude- 判準；兩者分歧會讓計價與歸因對不起來。"""
    assert ct.provider_of("claude-opus-5") == "anthropic"
    assert ct.provider_of("claude-sonnet-5") == "anthropic"
    assert ct.provider_of("gemini-flash-latest") == "gemini"
    assert ct.provider_of("gemini-pro-latest") == "gemini"


def test_record_cost_writes_aggregate_and_provider(fake_redis):
    ct.record_cost(10.0, provider="anthropic")
    ct.record_cost(2.5, provider="gemini")

    assert ct.month_total() == pytest.approx(12.5)     # 彙總＝預算閘門看的數字
    assert ct.today_total() == pytest.approx(12.5)
    by = ct.month_by_provider()
    assert by["anthropic"] == pytest.approx(10.0)
    assert by["gemini"] == pytest.approx(2.5)


def test_aggregate_still_written_without_provider(fake_redis):
    """漏帶 provider 時彙總桶**仍要寫**——否則預算閘門會少計而失守。"""
    ct.record_cost(7.0)
    assert ct.month_total() == pytest.approx(7.0)
    assert ct.month_by_provider() == {"gemini": 0.0, "anthropic": 0.0}


def test_unknown_provider_does_not_create_bucket(fake_redis):
    ct.record_cost(3.0, provider="openai")
    assert ct.month_total() == pytest.approx(3.0)
    assert ct.month_by_provider() == {"gemini": 0.0, "anthropic": 0.0}


def test_provider_sum_matches_aggregate_when_all_tagged(fake_redis):
    """全部標了 provider 時，兩邊要對得起來（差額＝有呼叫沒標）。"""
    for amount, provider in [(1.0, "gemini"), (20.0, "anthropic"), (0.5, "gemini")]:
        ct.record_cost(amount, provider=provider)
    by = ct.month_by_provider()
    assert sum(by.values()) == pytest.approx(ct.month_total())


def test_month_by_provider_empty_buckets_are_zero(fake_redis):
    """桶不存在時回 0，首頁渲染不能因此炸掉。"""
    assert ct.month_by_provider() == {"gemini": 0.0, "anthropic": 0.0}
