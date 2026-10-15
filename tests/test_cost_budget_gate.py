"""spec 009 測試基線：預算閘門、grounding 免費額、redis 故障、校準端點、/ask 逾限早退。

預算閘門壞掉不會報錯，只會靜默多花錢——所以每個分支都要釘住。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from cost import tracker as ct


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, float] = {}
        self.ttls: dict[str, int] = {}

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def incrbyfloat(self, key, amount):
        self.store[key] = self.store.get(key, 0.0) + float(amount)
        return self.store[key]

    def get(self, key):
        return None if key not in self.store else str(self.store[key])

    def set(self, key, value, keepttl=False):
        self.store[key] = float(value)
        if not keepttl:
            self.ttls.pop(key, None)
        return True

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def ttl(self, key):
        return self.ttls.get(key, -1)


class _BrokenRedis:
    def __getattr__(self, name):
        def boom(*_a, **_k):
            raise ConnectionError("redis down")
        return boom


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(ct, "redis_client", r)
    monkeypatch.setattr(ct.settings, "monthly_cost_limit_twd", 800)
    monkeypatch.setattr(ct.settings, "daily_cost_limit_twd", 45)
    return r


# ── T016 grounding：當月第 5,001 次起才計費 ──

def test_grounding_free_until_5000_then_billed(fake_redis):
    fake_redis.store[ct._grounding_key()] = ct._GROUNDING_FREE_PER_MONTH - 1
    assert ct.record_grounding_request() == 0.0                # 第 5,000 次仍免費
    paid = ct.record_grounding_request()                       # 第 5,001 次
    assert paid == pytest.approx(14.0 / 1000 * ct._USD_TWD)
    assert fake_redis.ttls[ct._grounding_key()] > 0            # 計數有 TTL，不會永久累積


# ── T017 check_budget 三分支 ──

def test_check_budget_month_full_blocks(fake_redis):
    fake_redis.store[ct._month_key()] = 800.0
    ok, reason, _, _ = ct.check_budget()
    assert not ok and "本月" in reason


def test_check_budget_day_full_blocks(fake_redis):
    fake_redis.store[ct._month_key()] = 100.0
    fake_redis.store[ct._day_key()] = 45.0
    ok, reason, spent, limit = ct.check_budget()
    assert not ok and "今日" in reason
    assert (spent, limit) == (45.0, 45.0)


def test_check_budget_both_under_passes(fake_redis):
    fake_redis.store[ct._month_key()] = 100.0
    fake_redis.store[ct._day_key()] = 10.0
    assert ct.check_budget() == (True, "", 10.0, 45.0)


# ── T018 set_month_total 覆寫並保留 TTL；redis 故障讀成 0、不拋 ──

def test_set_month_total_overwrites_and_restores_ttl(fake_redis):
    mk = ct._month_key()
    fake_redis.store[mk] = 123.0                               # 無 TTL 的舊桶
    assert ct.set_month_total(456.7) == 456.7
    assert fake_redis.ttls[mk] == 70 * 24 * 3600


def test_set_month_total_keeps_existing_ttl(fake_redis):
    mk = ct._month_key()
    fake_redis.store[mk] = 1.0
    fake_redis.ttls[mk] = 999
    ct.set_month_total(2.0)
    assert fake_redis.ttls[mk] == 999


def test_redis_failure_reads_zero_and_never_raises(monkeypatch):
    monkeypatch.setattr(ct, "redis_client", _BrokenRedis())
    assert ct.month_total() == 0.0 and ct.today_total() == 0.0
    ct.record_cost(1.0, provider="gemini")                     # 不拋
    assert ct.record_grounding_request() == 0.0


# ── T019 校準端點權杖：未設 → 503、錯 token → 401、對 → 覆寫 ──

def _calibrate(token):
    from api import brief
    return asyncio.run(brief.calibrate_cost(month_total_twd=321.0, x_admin_token=token))


def test_calibrate_disabled_without_admin_token(fake_redis, monkeypatch):
    monkeypatch.setattr(ct.settings, "admin_token", "")
    with pytest.raises(HTTPException) as exc:
        _calibrate("anything")
    assert exc.value.status_code == 503


def test_calibrate_rejects_wrong_token(fake_redis, monkeypatch):
    monkeypatch.setattr(ct.settings, "admin_token", "secret")
    for bad in (None, "wrong"):
        with pytest.raises(HTTPException) as exc:
            _calibrate(bad)
        assert exc.value.status_code == 401
    assert ct._month_key() not in fake_redis.store            # 被擋下就不能寫入


def test_calibrate_with_correct_token_overwrites(fake_redis, monkeypatch):
    monkeypatch.setattr(ct.settings, "admin_token", "secret")
    out = _calibrate("secret")
    assert out["after_twd"] == 321.0


# ── T020 /ask 逾限早退：不得發出任何 LLM 呼叫 ──

def test_ask_over_budget_returns_before_any_llm_call(fake_redis, monkeypatch):
    from api import ask as ask_api

    def must_not_call(*_a, **_k):
        raise AssertionError("逾限時不得呼叫 LLM")

    monkeypatch.setattr(ask_api.gemini_client, "classify_finance_intent", must_not_call)
    monkeypatch.setattr(ask_api, "get_decision_llm", must_not_call)
    monkeypatch.setattr(ask_api, "_ask_gemini", must_not_call)
    monkeypatch.setattr(ct.settings, "admin_token", "")
    fake_redis.store[ct._day_key()] = 45.0

    req = ask_api.AskRequest.model_validate({"question": "台積電怎麼看", "user_id": "u1"})
    resp = asyncio.run(ask_api.ask(req, x_admin_token=None))
    assert resp.budget_exceeded is True and resp.cost_twd == 0.0
    assert fake_redis.store[ct._day_key()] == 45.0            # 沒有多記任何花費
