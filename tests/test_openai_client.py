"""決策層 OpenAI client（spec 026 US1）。

釘住的都是「錯了不會有錯誤訊息、只會靜默產出爛晨報或亂花錢」的性質：
(a) strict schema：每個 object 全欄位 required、additionalProperties=false（否則 400）
(b) 工具迴圈：reasoning items 必須跟 function_call_output 一起送回（否則多輪推理斷掉）
(c) 輪數上限、incomplete（推理吃光輸出上限）、refusal 都不得當成功晨報
(d) 錯誤映射成中立例外，上層不認得 openai 型別
(e) usage 多輪加總、搜尋次數從 output items 數
(f) store=False、不平行呼叫工具、tools 順序固定

全程 mock SDK，不打任何外部 API。
"""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import httpx
import openai
import pytest

from ai import fetch_tool
from ai import openai_client as oc
from ai.errors import LLMBadRequest, LLMError, LLMQuotaExceeded, LLMRefused, LLMUnavailable
from ai.fetch_tool import FetchSession
from ai.schemas import BriefDraft

URL = "https://news.example.com/a?id=1"


def _usage(inp=1000, out=200, cached=0, write=0, reasoning=0):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        input_tokens_details=SimpleNamespace(cached_tokens=cached, cache_write_tokens=write),
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def _reasoning(rid="rs_1"):
    return SimpleNamespace(type="reasoning", id=rid, encrypted_content="enc", summary=[])


def _fcall(url=URL, call_id="call_1", name="web_fetch"):
    return SimpleNamespace(type="function_call", name=name, call_id=call_id,
                           arguments=json.dumps({"url": url}), id="fc_" + call_id)


def _search():
    return SimpleNamespace(type="web_search_call", id="ws_1", status="completed", action=None)


def _message(text):
    return SimpleNamespace(type="message", role="assistant",
                           content=[SimpleNamespace(type="output_text", text=text)])


def _resp(output, *, usage=None, status="completed", reason=None, model="gpt-6.1-sol-2026-10-01"):
    return SimpleNamespace(
        output=output, usage=usage or _usage(), status=status, model=model,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None, error=None,
    )


def _draft_json(**over):
    payload = {
        "headline": "今日結論", "sections": [], "tw_watchlist": [], "tw_caution": [],
        "risks": [], "follow_ups": [], "news_digest": [], "fact_checks": [],
        "data_as_of": "2026-10-22", "sources": [],
    }
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False)


class _FakeResponses:
    def __init__(self, responses):
        self._queue = list(responses)
        self.calls: list[dict] = []

    def create(self, **kw):
        # input 是 list，之後會被 client 端 extend——存快照才看得到當時送了什麼
        kw = dict(kw)
        kw["input"] = list(kw.get("input") or [])
        self.calls.append(kw)
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def fake(monkeypatch):
    def _install(responses):
        api = _FakeResponses(responses)
        monkeypatch.setattr(oc, "_client", lambda: SimpleNamespace(responses=api))
        return api
    return _install


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(fetch_tool.socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                      ("93.184.216.34", port))])
    body = "<html><body><p>" + "原文內容" * 100 + "</p></body></html>"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/html"},
                                   content=body.encode()))
    return FetchSession([URL], max_uses=3, max_chars=12_000, transport=transport)


def _structured(session=None, tools=None, search_limit=2):
    return oc.generate_structured(
        BriefDraft, system="sys", user_prompt="user", model="gpt-6.1-sol",
        tools=tools if tools is not None else oc.build_tools(fetch=True, search=True),
        effort="medium", fetch_session=session, search_limit=search_limit,
    )


# ── (a) schema ────────────────────────────────────────────────────────────

def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for v in node.values():
            yield from _objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _objects(v)


def test_schema_is_openai_strict():
    schema = oc.schema_of(BriefDraft)
    objs = list(_objects(schema))
    assert objs, "schema 裡應該有 object"
    for obj in objs:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    assert "$defs" in schema


def test_optional_field_becomes_nullable_and_null_is_stripped_before_validation():
    from pydantic import BaseModel

    class Inner(BaseModel):
        a: str
        tags: list[str] = []

    class Outer(BaseModel):
        name: str
        note: str = "預設"
        inner: Inner

    schema = oc.schema_of(Outer)
    assert set(schema["required"]) == {"name", "note", "inner"}
    assert {"type": "null"} in schema["properties"]["note"].get("anyOf", []) or \
        "null" in schema["properties"]["note"].get("type", [])

    raw = {"name": "x", "note": None, "inner": {"a": "y", "tags": None}}
    original = Outer.model_json_schema()
    cleaned = oc._strip_nulls(raw, original, original.get("$defs") or {})
    parsed = Outer.model_validate(cleaned)
    assert parsed.note == "預設" and parsed.inner.tags == []


def test_brief_draft_nullable_fields_keep_null():
    """原本就是 Optional 的欄位（source_ref 等）回 null 要保留，不是刪掉。"""
    raw = json.loads(_draft_json(news_digest=[{
        "title": "t", "source": "s", "date": "d", "url": None, "takeaway": "k", "uncertainty": None,
    }]))
    original = BriefDraft.model_json_schema()
    cleaned = oc._strip_nulls(raw, original, original["$defs"])
    assert cleaned["news_digest"][0]["url"] is None
    BriefDraft.model_validate(cleaned)


# ── (b) 工具迴圈 ──────────────────────────────────────────────────────────

def test_tool_loop_feeds_back_reasoning_and_function_output(fake, session):
    api = fake([
        _resp([_reasoning(), _fcall()], usage=_usage(inp=60_000, out=500, write=58_000)),
        _resp([_reasoning("rs_2"), _message(_draft_json())],
              usage=_usage(inp=64_000, out=3_000, cached=58_000, reasoning=2_000)),
    ])
    draft, usage, attempts, version = _structured(session)
    assert draft.headline == "今日結論"
    assert version == "gpt-6.1-sol-2026-10-01"

    second_input = api.calls[1]["input"]
    types = [getattr(i, "type", None) or (i.get("type") if isinstance(i, dict) else None)
             for i in second_input]
    # user → reasoning → function_call → function_call_output
    assert types[1:] == ["reasoning", "function_call", "function_call_output"]
    fco = second_input[-1]
    assert fco["call_id"] == "call_1"
    assert json.loads(fco["output"])["ok"] is True

    assert len(attempts) == 1 and attempts[0].ok
    assert usage["rounds"] == 2
    assert usage["input_tokens"] == 124_000
    assert usage["cached_tokens"] == 58_000
    assert usage["cache_write_tokens"] == 58_000
    assert usage["reasoning_tokens"] == 2_000
    assert usage["web_fetch_requests"] == 1


def test_non_whitelisted_url_is_rejected_and_recorded(fake, session):
    fake([
        _resp([_fcall(url="http://169.254.169.254/latest/meta-data")]),
        _resp([_message(_draft_json())]),
    ])
    _draft, _usage_, attempts, _v = _structured(session)
    assert attempts[0].error_code == "url_not_allowed"
    assert attempts[0].reject_reason == "not_in_facts_pack"


def test_unknown_function_name_gets_error_output(fake, session):
    api = fake([_resp([_fcall(name="shell")]), _resp([_message(_draft_json())])])
    _structured(session)
    out = json.loads(api.calls[1]["input"][-1]["output"])
    assert out["ok"] is False
    assert session.attempts == []


def test_search_calls_counted_and_quota_enforced_across_rounds(fake, session):
    api = fake([
        _resp([_search(), _search(), _fcall()]),
        _resp([_message(_draft_json())]),
    ])
    _d, usage, _a, _v = _structured(session, search_limit=2)
    assert usage["web_search_requests"] == 2
    assert api.calls[0]["max_tool_calls"] == 2
    # 第二輪搜尋額度已用完：用 allowed_tools 限縮，tools 清單本身不變（保住快取前綴）
    assert "max_tool_calls" not in api.calls[1]
    assert api.calls[1]["tool_choice"]["type"] == "allowed_tools"
    assert api.calls[1]["tools"] == api.calls[0]["tools"]


# ── (c) 失敗不得當成功 ────────────────────────────────────────────────────

def test_round_limit_raises_with_attempts(fake, session, monkeypatch):
    monkeypatch.setattr(oc.settings, "openai_max_tool_rounds", 2)
    fake([_resp([_fcall(call_id="c1")]), _resp([_fcall(call_id="c2")])])
    with pytest.raises(LLMError, match="輪") as ei:
        _structured(session)
    assert len(ei.value.fetch_attempts) == 1      # 同一 URL 第二次走快取，不新增 attempt


def test_incomplete_max_output_tokens_is_failure(fake, session):
    fake([_resp([_message('{"headline": "半')], status="incomplete", reason="max_output_tokens")])
    with pytest.raises(LLMError, match="max_output_tokens"):
        _structured(session)


def test_incomplete_after_fetch_keeps_attempts(fake, session):
    fake([_resp([_fcall()]), _resp([], status="incomplete", reason="max_output_tokens")])
    with pytest.raises(LLMError) as ei:
        _structured(session)
    assert len(ei.value.fetch_attempts) == 1


def test_refusal_maps_to_llm_refused(fake, session):
    msg = SimpleNamespace(type="message", role="assistant",
                          content=[SimpleNamespace(type="refusal", refusal="無法協助")])
    fake([_resp([msg])])
    with pytest.raises(LLMRefused):
        _structured(session)


def test_invalid_json_is_failure(fake, session):
    fake([_resp([_message("這不是 JSON")])])
    with pytest.raises(LLMError, match="JSON"):
        _structured(session)


def test_schema_mismatch_is_failure(fake, session):
    fake([_resp([_message('{"headline": 1}')])])
    with pytest.raises(LLMError, match="schema"):
        _structured(session)


def test_empty_output_is_failure(fake, session):
    fake([_resp([])])
    with pytest.raises(LLMError, match="空"):
        _structured(session)


# ── (d) 錯誤映射 ──────────────────────────────────────────────────────────

_REQ = httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status_err(cls, code):
    return cls("err", response=httpx.Response(code, request=_REQ), body=None)


@pytest.mark.parametrize("exc,expected", [
    (_status_err(openai.RateLimitError, 429), LLMQuotaExceeded),
    (_status_err(openai.BadRequestError, 400), LLMBadRequest),
    (openai.APIConnectionError(request=_REQ), LLMUnavailable),
    (openai.APITimeoutError(request=_REQ), LLMUnavailable),
    (_status_err(openai.InternalServerError, 503), LLMUnavailable),
    (_status_err(openai.AuthenticationError, 401), LLMBadRequest),
])
def test_error_mapping(fake, session, exc, expected):
    fake([exc])
    with pytest.raises(expected) as ei:
        _structured(session)
    assert not isinstance(ei.value, openai.OpenAIError)
    assert ei.value.fetch_attempts == []


def test_missing_key_raises_llm_error(monkeypatch):
    oc.reset_client()
    monkeypatch.setattr(oc.settings, "openai_api_key", "")
    with pytest.raises(LLMError, match="OPENAI_API_KEY 未設定"):
        _structured(None, tools=[])


# ── (f) 請求參數 ──────────────────────────────────────────────────────────

def test_request_params(fake, session, monkeypatch):
    monkeypatch.setattr(oc.settings, "openai_max_output_tokens", 32000)
    api = fake([_resp([_message(_draft_json())])])
    _structured(session)
    call = api.calls[0]
    assert call["store"] is False
    assert call["include"] == ["reasoning.encrypted_content"]
    assert call["parallel_tool_calls"] is False
    assert call["reasoning"] == {"effort": "medium"}
    assert call["max_output_tokens"] == 32000
    assert [t.get("name") or t["type"] for t in call["tools"]] == ["web_fetch", "web_search"]
    fmt = call["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert "temperature" not in call
    assert call["instructions"] == "sys"


def test_no_tools_omits_tool_params(fake):
    api = fake([_resp([_message(_draft_json())])])
    _structured(None, tools=[], search_limit=0)
    assert "tools" not in api.calls[0]
    assert "parallel_tool_calls" not in api.calls[0]


def test_generate_text_only_web_search(fake):
    api = fake([_resp([_search(), _message("答案")])])
    text, usage = oc.generate_text(system="s", user_prompt="u", model="gpt-6.1-sol",
                                   effort="medium", search_limit=2)
    assert text == "答案"
    assert usage["web_search_requests"] == 1
    assert api.calls[0]["tools"] == [{"type": "web_search"}]
