"""問答 prompt caching 開關（spec 022 WP4；2026-11-06 預設改開）。

原本預設關，理由是「chat 稀疏、跨題重用不到」。但單題內的 server-side 工具迴圈
每次 web_search/web_fetch 都把整個前綴重送一輪——未快取時 68k 前綴每輪約 NT$11。
所以只要用到一次工具，5m 快取（寫 1.25×、讀 0.1×）就回本。TTL 仍應從 5m 起，不是 1h。
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


def test_default_is_on_with_short_ttl():
    """預設開——見檔頭。檢查 Settings 預設值而非 .env，避免本機設定影響。"""
    from config import Settings
    assert Settings.model_fields["enable_claude_prompt_cache"].default is True
    assert Settings.model_fields["claude_cache_ttl"].default == "5m"
    assert Settings.model_fields["claude_chat_fetch_uses"].default == 2


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
