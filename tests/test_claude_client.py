"""決策層 Claude client（spec 022-llm-tiering）。

這裡釘住的都是「錯了不會有錯誤訊息、只會靜默產出爛晨報」的性質：
(a) 送出的 payload 不含 Opus 5 已移除的 sampling 參數（帶了直接 400）
(b) refusal 是 HTTP 200，必須在讀 content 前攔下來（否則 content 可能是空陣列）
(c) pause_turn 必須續跑——漏掉不會報錯，只會拿到內容少一半的晨報
(d) tools 清單逐字穩定（順序一變就打掉整個 prompt cache 前綴）
(e) web_fetch 不得開 citations（與 output_config.format 互斥，會 400）

全程 mock SDK，不打任何外部 API。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from ai import claude_client as cc
from ai.errors import LLMError, LLMRefused


def _block(**kw):
    return SimpleNamespace(**kw)


def _usage(inp=100, out=50, cache_read=0, cache_write=0):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
    )


def _msg(*, text="{}", stop="end_turn", usage=None, content=None, category=None):
    return SimpleNamespace(
        content=content if content is not None else [_block(type="text", text=text)],
        stop_reason=stop,
        usage=usage or _usage(),
        stop_details=SimpleNamespace(category=category) if category else None,
    )


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeMessages:
    def __init__(self, messages):
        self._queue = list(messages)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._queue) - 1)
        return _FakeStream(self._queue[idx])


class _FakeClient:
    def __init__(self, messages):
        self.messages = _FakeMessages(messages)


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    monkeypatch.setattr(cc.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(cc.settings, "claude_max_tokens", 4096)
    monkeypatch.setattr(cc.settings, "claude_effort", "high")
    monkeypatch.setattr(cc.settings, "claude_max_continuations", 3)
    monkeypatch.setattr(cc.monitor, "mark", lambda *a, **k: None)
    cc.reset_client()
    yield
    cc.reset_client()


def _install(monkeypatch, messages) -> _FakeClient:
    client = _FakeClient(messages)
    monkeypatch.setattr(cc, "_client", lambda: client)
    return client


class _Tiny(BaseModel):
    headline: str


# ── (a) sampling 參數 ────────────────────────────────────────────────────

def test_payload_has_no_sampling_params(monkeypatch):
    """Opus 5 移除了 temperature/top_p/top_k，帶了直接 400。

    Gemini 路徑用 temperature=0.4/0.1，平移過來就會炸——這條測試就是防那個手滑。
    """
    client = _install(monkeypatch, [_msg(text='{"headline": "hi"}')])
    cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    sent = client.messages.calls[0]
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in sent, f"payload 不該帶 {banned}（Opus 5 會 400）"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"]["effort"] == "high"


def test_no_assistant_prefill(monkeypatch):
    """最後一則不得是 assistant（prefill 在 Opus 5 回 400）。"""
    client = _install(monkeypatch, [_msg(text='{"headline": "hi"}')])
    cc.generate_structured(_Tiny, system="s", user_prompt="u",
                           model="claude-opus-5", tools=[])
    assert client.messages.calls[0]["messages"][-1]["role"] == "user"


# ── (b) refusal ─────────────────────────────────────────────────────────

def test_refusal_raises_before_reading_content(monkeypatch):
    """refusal 是 HTTP 200 + 空 content；直接讀 content[0] 會炸，必須先攔。"""
    _install(monkeypatch, [_msg(stop="refusal", content=[], category="cyber")])
    with pytest.raises(LLMRefused):
        cc.generate_structured(_Tiny, system="s", user_prompt="u",
                               model="claude-opus-5", tools=[])


# ── (c) pause_turn ──────────────────────────────────────────────────────

def test_pause_turn_continues_and_finishes(monkeypatch):
    """server tool 迭代上限 → pause_turn；必須把 assistant turn 接回去再送一次。"""
    paused = _msg(text="部分內容", stop="pause_turn", usage=_usage(inp=100, out=50))
    done = _msg(text='{"headline": "完成"}', stop="end_turn", usage=_usage(inp=200, out=80))
    client = _install(monkeypatch, [paused, done])

    result, usage = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )

    assert result.headline == "完成"
    assert len(client.messages.calls) == 2, "pause_turn 必須續跑，不能當成完成"
    # 第二輪的對話要帶上第一輪那段未完成的 assistant turn，模型才知道從哪續
    second_convo = client.messages.calls[1]["messages"]
    assert second_convo[-1]["role"] == "assistant"
    # usage 跨輪累加（每輪都要重送整個對話，成本是疊上去的）
    assert usage["input_tokens"] == 300
    assert usage["output_tokens"] == 130


def test_pause_turn_converges_at_max_continuations(monkeypatch):
    """一直 pause_turn 不能變成無窮迴圈——收斂後 raise，讓上層走 Gemini 降級。"""
    client = _install(monkeypatch, [_msg(stop="pause_turn")])
    with pytest.raises(LLMError, match="pause_turn"):
        cc.generate_structured(_Tiny, system="s", user_prompt="u",
                               model="claude-opus-5", tools=[])
    # max_continuations=3 → 最多 4 輪
    assert len(client.messages.calls) == 4


# ── (d)(e) 工具設定 ──────────────────────────────────────────────────────

def test_tools_are_stable_and_fetch_has_no_citations():
    """tools 渲染在 prompt 最前面，內容或順序一變就打掉整個 cache 前綴。"""
    first = cc.build_tools(12, 5)
    second = cc.build_tools(12, 5)
    assert first == second

    assert [t["name"] for t in first] == ["web_fetch", "web_search"]
    fetch, search = first
    assert fetch["max_uses"] == 12 and search["max_uses"] == 5
    # citations 與 output_config.format 互斥，同開直接 400
    assert "citations" not in fetch
    # max_content_tokens 防單頁爆量（一頁新聞可能數千 token，×12 很可觀）
    assert fetch["max_content_tokens"] > 0


def test_no_code_execution_tool():
    """_20260209 版內建 dynamic filtering，再宣告 code_execution 會有兩個執行環境。"""
    types = {t["type"] for t in cc.build_tools(4, 2)}
    assert not any("code_execution" in t for t in types)


def test_web_search_requests_counted_from_content(monkeypatch):
    """web_search 按次計費；從 content blocks 自己數，不依賴 usage 上的欄位名。"""
    content = [
        _block(type="server_tool_use", name="web_search"),
        _block(type="server_tool_use", name="web_fetch"),     # fetch 不另計費
        _block(type="server_tool_use", name="web_search"),
        _block(type="text", text='{"headline": "x"}'),
    ]
    _install(monkeypatch, [_msg(content=content)])
    _result, usage = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert usage["web_search_requests"] == 2


# ── schema ──────────────────────────────────────────────────────────────

def test_schema_is_strictified():
    """structured outputs 要求每個 object 節點都有 additionalProperties: false。"""
    schema = cc.schema_of(_Tiny)
    assert schema["additionalProperties"] is False


def test_usage_normalisation_keeps_cache_fields_separate(monkeypatch):
    """Anthropic 的 input_tokens **不含** cache——與 Gemini 相反，計價端靠這點分流。"""
    _install(monkeypatch, [_msg(text='{"headline": "x"}',
                                usage=_usage(inp=100, out=50, cache_read=900, cache_write=40))])
    _result, usage = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert usage["input_tokens"] == 100          # 未含 cached
    assert usage["cached_tokens"] == 900
    assert usage["cache_write_tokens"] == 40


def test_bad_json_raises_llm_error(monkeypatch):
    _install(monkeypatch, [_msg(text="這不是 JSON")])
    with pytest.raises(LLMError):
        cc.generate_structured(_Tiny, system="s", user_prompt="u",
                               model="claude-opus-5", tools=[])
