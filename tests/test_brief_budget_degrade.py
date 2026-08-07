"""晨報預算降級（2026-08-07）。

晨報刻意**不受** `check_budget()` 攔截（那只擋 /ask），因為它是產品本體、不能因額度見底
而消失。但「不攔截」一度等於「無聲吃光整個月的額度」——2026-08-06 當天跑了三次晨報、
單日 NT$115.19（當時日上限 NT$30），完全沒被擋下。

這裡釘住的是那個中間選項：額度快見底時**降級但仍出報**，而不是二選一。
降級的內容也很重要——要關就整個關，因為「部分查證」比「不查證」更糟（看起來像查過了）。
"""
from __future__ import annotations

import pytest

from cost import tracker as ct


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


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(ct, "redis_client", r)
    monkeypatch.setattr(ct.settings, "monthly_cost_limit_twd", 600)
    monkeypatch.setattr(ct.settings, "daily_cost_limit_twd", 45)
    return r


# ── 門檻 ────────────────────────────────────────────────────────────────

def test_month_remaining_tracks_spend(fake_redis):
    assert ct.month_remaining() == pytest.approx(600.0)
    ct.record_cost(120.0, provider="anthropic")
    assert ct.month_remaining() == pytest.approx(480.0)


def test_no_degrade_while_budget_is_healthy(fake_redis):
    ct.record_cost(400.0, provider="anthropic")   # 剩 200，遠高於日上限
    assert ct.brief_should_degrade() is False


def test_degrade_when_remaining_below_one_day(fake_redis):
    """門檻用日上限當代理值：日上限的語意就是「一篇晨報 + 若干問答」。"""
    ct.record_cost(560.0, provider="anthropic")   # 剩 40 < 45
    assert ct.brief_should_degrade() is True


def test_degrade_when_already_overspent(fake_redis):
    ct.record_cost(700.0, provider="anthropic")   # 剩 -100
    assert ct.month_remaining() < 0
    assert ct.brief_should_degrade() is True


# ── 降級的內容 ──────────────────────────────────────────────────────────

def test_frugal_brief_drops_tools_facts_and_cache(monkeypatch):
    """節儉模式＝關工具 + 清空 facts pack + effort 最低 + 關 prompt cache。

    最後一項容易漏：沒有工具就只有一輪，前綴不會被重讀，1.25× 的 cache write 是純虧。
    """
    from ai import llm_client

    captured: dict = {}

    def _fake_generate_structured(model_cls, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        from ai.schemas import BriefDraft
        return BriefDraft.model_construct(fact_checks=[]), {"input_tokens": 0}

    monkeypatch.setattr(llm_client.claude_client, "generate_structured",
                        _fake_generate_structured)
    monkeypatch.setattr(llm_client.settings, "enable_claude_brief_prompt_cache", True)
    monkeypatch.setattr(llm_client.settings, "claude_brief_effort", "medium")

    llm = llm_client.AnthropicDecisionLLM()
    llm.draft_brief({"as_of": "2026-08-07"}, '[{"title": "x"}]', None, frugal=True)

    assert captured["tools"] == [], "有工具＝會付查證的錢"
    assert captured["effort"] == "low"
    assert captured["cacheable"] is False, "只有一輪時 cache write 是純虧"
    assert "本次無外部線索" in captured["user_prompt"], "facts pack 要清空"
    # 沒有工具就不能留查證契約，否則模型會去呼叫不存在的工具
    assert "## 查證契約" not in captured["system"]
    assert "本次無連網查證工具" in captured["system"]
    # FULL_BRIEF_RULES 第 1 條本來就指向 facts pack／查證契約——沒有明說「本次不適用」
    # 就會留下一條懸空引用，而互相矛盾的指令正是模型行為不可預期的來源。
    assert "本次**不適用**" in captured["system"]


def test_normal_brief_keeps_tools_and_brief_effort(monkeypatch):
    """常態路徑不受降級影響，且 effort 走晨報那一檔（不是問答的 CLAUDE_EFFORT）。"""
    from ai import llm_client

    captured: dict = {}

    def _fake_generate_structured(model_cls, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        from ai.schemas import BriefDraft
        return BriefDraft.model_construct(fact_checks=[]), {"input_tokens": 0}

    monkeypatch.setattr(llm_client.claude_client, "generate_structured",
                        _fake_generate_structured)
    monkeypatch.setattr(llm_client.settings, "enable_claude_brief_prompt_cache", True)
    monkeypatch.setattr(llm_client.settings, "claude_brief_effort", "medium")
    monkeypatch.setattr(llm_client.settings, "claude_effort", "high")

    llm_client.AnthropicDecisionLLM().draft_brief({"as_of": "2026-08-07"}, "[]", None)

    assert [t["name"] for t in captured["tools"]] == ["web_fetch", "web_search"]
    assert captured["effort"] == "medium"
    assert captured["cacheable"] is True
    assert "## 查證契約" in captured["system"]
