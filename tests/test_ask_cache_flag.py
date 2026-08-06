"""問答 prompt caching 開關（spec 022 WP4）。

⚠️ 這裡釘住的是一個**違反直覺**的結論：chat 用量稀疏時，開 prompt caching 會讓成本**變高**。

Anthropic 快取寫入付 1.25×(5m) / 2×(1h) 的 input 價、讀取才 0.1×，
5m 需 2 次、1h 需 3 次以上讀取才回本。一天只問一兩題且分散在不同時段時，
每題都是「寫入後從未被讀取就過期」＝每題多付 25–100% input 費用換零收益。

這跟 Gemini 的 cachedContents 直覺相反（那個是先付一筆存起來、按存放時間計費），
所以預設必須是關的，且要有測試防止有人「順手打開優化一下」。
"""
from __future__ import annotations

import pytest

from ai import llm_client
from config import settings


@pytest.fixture
def _captured(monkeypatch):
    calls: list[dict] = []

    def _fake(system_blocks, user_prompt, model, tools):
        calls.append({"system_blocks": system_blocks, "model": model, "tools": tools})
        return "答案", {"input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(llm_client.claude_client, "generate_answer",
                        lambda **kw: _fake(**kw))
    return calls


def test_default_is_off():
    """預設關閉——見檔頭的成本說明。改這個預設前請先看 log 的 cache_read_input_tokens。"""
    assert settings.enable_claude_prompt_cache is False
    assert settings.claude_cache_ttl == "5m"   # 開的話也該從 5m 起，不是 1h


def test_no_cache_control_when_disabled(monkeypatch, _captured):
    monkeypatch.setattr(llm_client.settings, "enable_claude_prompt_cache", False)
    llm_client.AnthropicDecisionLLM().answer_question("system", "user", cacheable=True)
    block = _captured[0]["system_blocks"][0]
    assert "cache_control" not in block, "關閉時仍掛 cache_control＝無聲支付寫入 premium"


def test_cache_control_present_when_enabled(monkeypatch, _captured):
    monkeypatch.setattr(llm_client.settings, "enable_claude_prompt_cache", True)
    monkeypatch.setattr(llm_client.settings, "claude_cache_ttl", "1h")
    llm_client.AnthropicDecisionLLM().answer_question("system", "user", cacheable=True)
    block = _captured[0]["system_blocks"][0]
    assert block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_not_cacheable_call_never_gets_cache_control(monkeypatch, _captured):
    monkeypatch.setattr(llm_client.settings, "enable_claude_prompt_cache", True)
    llm_client.AnthropicDecisionLLM().answer_question("system", "user", cacheable=False)
    assert "cache_control" not in _captured[0]["system_blocks"][0]


def test_chat_uses_lower_tool_budget(monkeypatch, _captured):
    """問答是互動情境，對延遲敏感——查證額度要比晨報低。"""
    monkeypatch.setattr(llm_client.settings, "claude_chat_fetch_uses", 4)
    monkeypatch.setattr(llm_client.settings, "claude_chat_search_uses", 2)
    llm_client.AnthropicDecisionLLM().answer_question("system", "user", cacheable=True)
    tools = {t["name"]: t["max_uses"] for t in _captured[0]["tools"]}
    assert tools == {"web_fetch": 4, "web_search": 2}


def test_intent_classifier_stays_on_gemini():
    """意圖分類是 trivial gate、fail-open、token 極少，用決策層模型是純浪費。"""
    assert "gemini" in settings.gemini_model_classifier
    assert not settings.gemini_model_classifier.startswith("claude")
