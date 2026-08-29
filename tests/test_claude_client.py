"""決策層 Claude client（spec 022-llm-tiering）。

這裡釘住的都是「錯了不會有錯誤訊息、只會靜默產出爛晨報」的性質：
(a) 送出的 payload 不含 Opus 5 已移除的 sampling 參數（帶了直接 400）
(b) refusal 是 HTTP 200，必須在讀 content 前攔下來（否則 content 可能是空陣列）
(c) pause_turn 必須續跑——漏掉不會報錯，只會拿到內容少一半的晨報
(d) tools 清單逐字穩定（順序一變就打掉整個 prompt cache 前綴）
(e) web_fetch 不得開 citations（與 output_config.format 互斥，會 400）
(f) 查證 attempts 的配對（spec 023）——錯了不會報錯，只會讓失敗原因靜默消失

全程 mock SDK，不打任何外部 API。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from ai import claude_client as cc
from ai.errors import LLMError, LLMRefused


def _fetch_use(tool_id: str, url: str):
    return _block(type="server_tool_use", name="web_fetch", id=tool_id, input={"url": url})


def _fetch_ok(tool_id: str, url: str, retrieved_at="2026-08-29T00:00:00Z"):
    return _block(
        type="web_fetch_tool_result", tool_use_id=tool_id,
        content=_block(type="web_fetch_result", url=url, retrieved_at=retrieved_at),
    )


def _fetch_err(tool_id: str, error_code: str):
    """錯誤 block **沒有 url 欄位**——這正是必須靠 tool_use_id 回頭配對的原因。"""
    return _block(
        type="web_fetch_tool_result", tool_use_id=tool_id,
        content=_block(type="web_fetch_tool_result_error", error_code=error_code),
    )


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


def test_effort_is_overridable_per_call(monkeypatch):
    """晨報與問答分開設 effort——thinking token 以 output 費率計價，這是輸出端的成本主閥。

    2026-08-07 實測 effort=high 跑出 output 32,236 tokens ≈ NT$25.8＝單篇決策成本的 70%。
    """
    client = _install(monkeypatch, [_msg(text='{"headline": "hi"}')])
    cc.generate_structured(_Tiny, system="s", user_prompt="u",
                           model="claude-opus-5", tools=[], effort="medium")
    assert client.messages.calls[0]["output_config"]["effort"] == "medium"
    # 沒指定就回落到全域設定（問答路徑不受晨報那一檔影響）
    cc.generate_structured(_Tiny, system="s", user_prompt="u",
                           model="claude-opus-5", tools=[])
    assert client.messages.calls[1]["output_config"]["effort"] == "high"


def test_thinking_is_never_disabled(monkeypatch):
    """Opus 5 關掉 thinking 有兩個靜默失效模式（工具呼叫被寫成純文字、標籤外洩）。

    省錢請降 effort。這條測試是防「為了省錢把 thinking 關掉」那個手滑。
    """
    client = _install(monkeypatch, [_msg(text='{"headline": "hi"}')])
    cc.generate_structured(_Tiny, system="s", user_prompt="u",
                           model="claude-opus-5", tools=[], effort="low")
    assert client.messages.calls[0]["thinking"]["type"] == "adaptive"


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

    result, usage, _attempts = cc.generate_structured(
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


def test_brief_prompt_is_cached_by_default(monkeypatch):
    """晨報要開快取——工具迴圈在單一請求內把同一個 ~55k 前綴重讀多輪，讀取只要 0.1×。

    2026-08-06 實測：沒開快取 + max_uses=12 → NT$73.83/篇（≈NT$1,550/月）。
    """
    monkeypatch.setattr(cc.settings, "claude_cache_ttl", "5m")
    client = _install(monkeypatch, [_msg(text='{"headline": "hi"}')])
    cc.generate_structured(_Tiny, system="s", user_prompt="u",
                           model="claude-opus-5", tools=[], cacheable=True)
    block = client.messages.calls[0]["messages"][0]["content"][0]
    assert block["cache_control"] == {"type": "ephemeral", "ttl": "5m"}


def test_no_cache_control_when_not_cacheable(monkeypatch):
    client = _install(monkeypatch, [_msg(text='{"headline": "hi"}')])
    cc.generate_structured(_Tiny, system="s", user_prompt="u",
                           model="claude-opus-5", tools=[], cacheable=False)
    block = client.messages.calls[0]["messages"][0]["content"][0]
    assert "cache_control" not in block


def test_fetch_content_cap_is_configurable(monkeypatch):
    """單頁擷取上限：長報導會跟著後續每一輪工具迴圈重送，故要能壓。"""
    monkeypatch.setattr(cc.settings, "claude_fetch_max_content_tokens", 3000)
    fetch = cc.build_tools(5, 3)[0]
    assert fetch["max_content_tokens"] == 3000
    assert fetch["max_uses"] == 5


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
    _result, usage, _attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert usage["web_search_requests"] == 2


def test_web_fetch_requests_counted_even_though_unbilled(monkeypatch):
    """`web_fetch` 不按次計費，但**必須**照數——這是查證層唯一的可觀測性來源。

    不計費代表查證層跑或不跑在帳單上完全看不出來：2026-08-12 盤點時連續多天出現
    「fact_checks 全部 unverifiable」，卻無法分辨是「查了但查不到」還是「根本沒查」。
    ⚠️ 別拿 usage.tool_tokens 當代理值——Anthropic 那欄恆為 0（見 `_usage_of`）。
    """
    content = [
        _block(type="server_tool_use", name="web_fetch"),
        _block(type="server_tool_use", name="web_search"),
        _block(type="server_tool_use", name="web_fetch"),
        _block(type="server_tool_use", name="web_fetch"),
        _block(type="text", text='{"headline": "x"}'),
    ]
    _install(monkeypatch, [_msg(content=content)])
    _result, usage, _attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert usage["web_fetch_requests"] == 3
    assert usage["web_search_requests"] == 1
    assert usage["tool_tokens"] == 0, "Anthropic 不分開回報 tool token，不可當查證代理值"


def test_tool_counts_survive_pause_turn_continuations(monkeypatch):
    """續跑時次數要累加，不是只算最後一輪——否則多輪查證的用量被低估。"""
    paused = _msg(
        content=[_block(type="server_tool_use", name="web_fetch"), _block(type="text", text="半途")],
        stop="pause_turn",
    )
    done = _msg(content=[
        _block(type="server_tool_use", name="web_fetch"),
        _block(type="text", text='{"headline": "完成"}'),
    ])
    _install(monkeypatch, [paused, done])
    _result, usage, _attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert usage["web_fetch_requests"] == 2
    assert usage["rounds"] == 2


# ── schema ──────────────────────────────────────────────────────────────

def test_source_ref_hint_lists_only_real_paths():
    """2026-08-06 實測：模型會捏造看似合理的路徑，guardrail 整條丟掉（37 條丟 14 條）。

    提示裡列的必須是 features 內**真實存在**的葉節點，且要明說「不確定就留 null」——
    guardrail 對 null 是放行的，猜錯才會被丟。
    """
    from ai import prompts

    features = {
        "as_of": "2026-08-05",
        "tw": {"index": {"close": 24000.0, "ma20": 23600.0},
               "stocks": {"2330": {"close": 1200.0, "amount": 5e9}}},
        "us_crypto": {"assets": {"SOX": {"return_20d_pct": 22.1}}},
    }
    hint = prompts._source_ref_hint(features)
    assert "features.tw.index.close" in hint
    assert "null" in hint                      # 明說可以留空
    assert "整條 evidence 被丟棄" in hint       # 明說捏造的後果
    # 列出的每一條都必須真的解析得到
    from guardrails.verify import _MISSING, _resolve
    listed = [ln[2:] for ln in hint.splitlines() if ln.startswith("- features.")]
    assert listed, "應該要列出真實路徑範例"
    for path in listed:
        assert _resolve(features, path) is not _MISSING, f"提示列了不存在的路徑 {path}"


def test_source_ref_hint_empty_features_is_noop():
    from ai import prompts
    assert prompts._source_ref_hint({}) == ""


def test_schema_is_strictified():
    """structured outputs 要求每個 object 節點都有 additionalProperties: false。"""
    schema = cc.schema_of(_Tiny)
    assert schema["additionalProperties"] is False


def test_usage_normalisation_keeps_cache_fields_separate(monkeypatch):
    """Anthropic 的 input_tokens **不含** cache——與 Gemini 相反，計價端靠這點分流。"""
    _install(monkeypatch, [_msg(text='{"headline": "x"}',
                                usage=_usage(inp=100, out=50, cache_read=900, cache_write=40))])
    _result, usage, _attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert usage["input_tokens"] == 100          # 未含 cached
    assert usage["cached_tokens"] == 900
    assert usage["cache_write_tokens"] == 40


def test_usage_reports_round_count(monkeypatch):
    """`rounds` 是 prompt cache 的損益判準：cache write 付 1.25×，只有被後續輪次
    重讀才回本。rounds=1 且 cached≈0 ⇒ 寫了沒被讀 ⇒ 開快取是純虧。不記就只能猜。
    """
    paused = _msg(text="半途", stop="pause_turn")
    done = _msg(text='{"headline": "完成"}', stop="end_turn")
    _install(monkeypatch, [paused, done])
    _result, usage, _attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert usage["rounds"] == 2

    _install(monkeypatch, [_msg(text='{"headline": "一輪就完"}')])
    _result, usage, _attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert usage["rounds"] == 1


def test_bad_json_raises_llm_error(monkeypatch):
    _install(monkeypatch, [_msg(text="這不是 JSON")])
    with pytest.raises(LLMError):
        cc.generate_structured(_Tiny, system="s", user_prompt="u",
                               model="claude-opus-5", tools=[])


# ── (f) 查證 attempts（spec 023 US1）─────────────────────────────────────

def test_fetch_attempt_pairs_success_with_url(monkeypatch):
    """成功的 attempt 要帶得出 url 與 retrieved_at——那是「這則線索真的查過」的唯一證據。"""
    content = [
        _fetch_use("t1", "https://example.com/a"),
        _fetch_ok("t1", "https://example.com/a"),
        _block(type="text", text='{"headline": "x"}'),
    ]
    _install(monkeypatch, [_msg(content=content)])
    _result, _usage, attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert len(attempts) == 1
    assert attempts[0].ok is True
    assert attempts[0].url == "https://example.com/a"
    assert attempts[0].error_code is None
    assert attempts[0].retrieved_at


def test_fetch_attempt_takes_url_from_request_when_failed(monkeypatch):
    """失敗時 URL 只能從 server_tool_use.input 拿——錯誤 block 只有 error_code。

    「哪個來源失敗」是 spec 023 US4 的判斷依據；配對寫錯就只剩一個沒有主詞的錯誤碼。
    """
    content = [
        _fetch_use("t1", "https://blocked.example/story"),
        _fetch_err("t1", "url_not_accessible"),
        _fetch_use("t2", "https://example.com/ok"),
        _fetch_ok("t2", "https://example.com/ok"),
        _block(type="text", text='{"headline": "x"}'),
    ]
    _install(monkeypatch, [_msg(content=content)])
    _result, _usage, attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    failed = [a for a in attempts if not a.ok]
    assert len(failed) == 1
    assert failed[0].url == "https://blocked.example/story"
    assert failed[0].error_code == "url_not_accessible"


def test_fetch_attempt_marks_max_uses_exceeded(monkeypatch):
    """`max_uses_exceeded` 是假說 A（額度不足）唯一的直接證據，不可與其他失敗混為一談。"""
    content = [
        _fetch_use("t1", "https://example.com/a"),
        _fetch_err("t1", "max_uses_exceeded"),
        _block(type="text", text='{"headline": "x"}'),
    ]
    _install(monkeypatch, [_msg(content=content)])
    _result, _usage, attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert [a.error_code for a in attempts] == ["max_uses_exceeded"]


def test_unmatched_fetch_request_is_still_recorded(monkeypatch):
    """請求了卻沒有結果 block（pause_turn 切在工具中間）也要留紀錄。

    不記的話 attempts 數會少於 web_fetch_requests，事後只會誤以為配對邏輯壞了。
    """
    content = [
        _fetch_use("t1", "https://example.com/dangling"),
        _block(type="text", text='{"headline": "x"}'),
    ]
    _install(monkeypatch, [_msg(content=content)])
    _result, usage, attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert len(attempts) == usage["web_fetch_requests"] == 1
    assert attempts[0].ok is False and attempts[0].error_code == "no_result"


def test_fetch_attempts_accumulate_across_pause_turn(monkeypatch):
    """續跑時每輪都有自己的 content blocks——只讀最後一輪會漏掉前面查過的來源。"""
    paused = _msg(
        content=[_fetch_use("t1", "https://a.example/1"), _fetch_ok("t1", "https://a.example/1"),
                 _block(type="text", text="半途")],
        stop="pause_turn",
    )
    done = _msg(content=[
        _fetch_use("t2", "https://b.example/2"), _fetch_err("t2", "too_many_requests"),
        _block(type="text", text='{"headline": "完成"}'),
    ])
    _install(monkeypatch, [paused, done])
    _result, usage, attempts = cc.generate_structured(
        _Tiny, system="s", user_prompt="u", model="claude-opus-5", tools=[],
    )
    assert [a.url for a in attempts] == ["https://a.example/1", "https://b.example/2"]
    assert [a.ok for a in attempts] == [True, False]
    assert usage["web_fetch_requests"] == 2


def test_refusal_still_carries_fetch_attempts(monkeypatch):
    """refusal 走 raise，區域變數會連同 usage 一起消失——attempts 必須掛在例外上帶出來。

    不帶出來的話，失敗那次的查證用量就從遙測裡整段消失（正是 spec 023 要消滅的盲點）。
    """
    content = [
        _fetch_use("t1", "https://example.com/a"),
        _fetch_err("t1", "url_not_allowed"),
    ]
    _install(monkeypatch, [_msg(content=content, stop="refusal", category="cyber")])
    with pytest.raises(LLMRefused) as excinfo:
        cc.generate_structured(_Tiny, system="s", user_prompt="u",
                               model="claude-opus-5", tools=[])
    attempts = excinfo.value.fetch_attempts
    assert [a.error_code for a in attempts] == ["url_not_allowed"]


def test_exhausted_continuations_carry_fetch_attempts(monkeypatch):
    """pause_turn 用盡那條例外路徑同樣要帶出 attempts。"""
    paused = _msg(
        content=[_fetch_use("t1", "https://example.com/a"), _fetch_ok("t1", "https://example.com/a")],
        stop="pause_turn",
    )
    _install(monkeypatch, [paused])
    with pytest.raises(LLMError, match="pause_turn") as excinfo:
        cc.generate_structured(_Tiny, system="s", user_prompt="u",
                               model="claude-opus-5", tools=[])
    assert len(excinfo.value.fetch_attempts) == 4   # max_continuations=3 → 4 輪各一次


def test_answer_path_does_not_break_on_attempts(monkeypatch):
    """問答路徑刻意丟棄 attempts，但不得因此改變它的回傳形狀。"""
    _install(monkeypatch, [_msg(text="回答內容")])
    text, usage = cc.generate_answer(
        system_blocks=[{"type": "text", "text": "s"}], user_prompt="u",
        model="claude-opus-5", tools=[],
    )
    assert text == "回答內容"
    assert usage["web_fetch_requests"] == 0
