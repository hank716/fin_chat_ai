"""OpenAI 決策 + 查證層（spec 026，方案 B）。

與 `claude_client` 職責相同：吃 features + facts pack + 校準，查證後產出結構化晨報草稿。
走官方 `openai` SDK 的 **Responses API**（Chat Completions 不支援所需的工具組合）。

## 與 Claude 路徑的關鍵差異（改動前先讀）

1. **查證工具在本地執行**。OpenAI 沒有「只能開對話中出現過的 URL」的抓頁工具，所以
   `web_fetch` 宣告成 function tool，由 `fetch_tool.FetchSession` 執行：白名單＝當篇 facts
   pack 的 URL、擋私有位址。工具名刻意與 Claude 相同，`prompts` 一字不改——A/B 只差模型。
2. **工具迴圈在我們手上**。Anthropic 的 server tool 迴圈跑在 API 內部（才有 pause_turn）；
   這裡是 client 端迴圈：有 function_call 就執行、把整段 `response.output`（含 reasoning
   items）接回 input 再送。**reasoning items 必須跟 tool output 一起送回**，否則多輪推理會斷。
   總輪數上限 `openai_max_tool_rounds`，防失控。
3. **`store=False` + `include=["reasoning.encrypted_content"]`**：不讓晨報內容留在 OpenAI
   端，也不依賴 `previous_response_id`（那需要 store=True）。
4. **strict 結構化輸出**要求每個 object 的所有 property 都列進 `required`、
   `additionalProperties: false`；選填欄位改成可為 null。見 `schema_of`。
5. **計價語意第三種**：`input_tokens` 是總數，cached 與 cache write 互斥——見 `cost.tracker`。
6. **不要再包 tenacity**——SDK 自己有 `max_retries`（同 claude_client 的教訓）。
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from activity import monitor
from config import settings

from .errors import (
    LLMBadRequest,
    LLMError,
    LLMQuotaExceeded,
    LLMRefused,
    LLMUnavailable,
)
from .fetch_tool import FetchAttempt, FetchSession

logger = logging.getLogger("ai-market-backend.openai")

_MAX_RETRIES = 2
_TIMEOUT_SECONDS = 900.0

_client_singleton: Any = None


def _client() -> Any:
    global _client_singleton
    if not settings.openai_api_key.strip():
        raise LLMError("OPENAI_API_KEY 未設定")
    if _client_singleton is None:
        import openai   # 延遲匯入：沒選 openai 的部署不必為它付啟動成本

        _client_singleton = openai.OpenAI(
            api_key=settings.openai_api_key,
            max_retries=_MAX_RETRIES,
            timeout=_TIMEOUT_SECONDS,
        )
    return _client_singleton


def reset_client() -> None:
    """測試用：清掉 singleton，讓下次呼叫依當前 settings 重建。"""
    global _client_singleton
    _client_singleton = None


# 宣告順序固定（web_fetch 在前、web_search 在後）：tools 在 prompt 最前面，換序會打掉快取前綴。
WEB_FETCH_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "web_fetch",
    "description": "開啟 facts pack 中列出的來源網址並回傳原文純文字。只能開啟 facts pack 內的 url。",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "facts pack 中某則 event 的 url"},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}
WEB_SEARCH_TOOL: dict[str, Any] = {"type": "web_search"}


def build_tools(*, fetch: bool, search: bool) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if fetch:
        tools.append(copy.deepcopy(WEB_FETCH_TOOL))
    if search:
        tools.append(dict(WEB_SEARCH_TOOL))
    return tools


# ── 結構化輸出 schema ─────────────────────────────────────────────────────

def _is_nullable(node: dict[str, Any]) -> bool:
    if node.get("type") == "null":
        return True
    if isinstance(node.get("type"), list) and "null" in node["type"]:
        return True
    return any(isinstance(b, dict) and b.get("type") == "null" for b in node.get("anyOf") or [])


def _make_nullable(node: dict[str, Any]) -> dict[str, Any]:
    if _is_nullable(node):
        return node
    if "anyOf" in node:
        node["anyOf"] = list(node["anyOf"]) + [{"type": "null"}]
        return node
    if "$ref" in node:
        return {"anyOf": [node, {"type": "null"}]}
    if isinstance(node.get("type"), str):
        node["type"] = [node["type"], "null"]
        return node
    return {"anyOf": [node, {"type": "null"}]}


def _strictify(node: Any) -> Any:
    """遞迴補成 OpenAI strict 形式：每個 object 全欄位 required、`additionalProperties: false`。

    原本非必填的欄位改成可為 null（strict 模式不允許「可以不給」，只允許「給 null」）。
    `default` 鍵一併拿掉——strict 模式不支援，留著會被 400。
    """
    if isinstance(node, list):
        return [_strictify(n) for n in node]
    if not isinstance(node, dict):
        return node
    node.pop("default", None)
    for key in ("$defs", "definitions"):
        if key in node:
            node[key] = {name: _strictify(sub) for name, sub in node[key].items()}
    if node.get("type") == "object" or "properties" in node:
        props = node.get("properties") or {}
        required = set(node.get("required") or [])
        new_props = {}
        for name, sub in props.items():
            sub = _strictify(sub)
            if name not in required:
                sub = _make_nullable(sub)
            new_props[name] = sub
        node["properties"] = new_props
        node["required"] = list(new_props)
        node["additionalProperties"] = False
    for key in ("items", "anyOf", "allOf", "oneOf"):
        if key in node:
            node[key] = _strictify(node[key])
    return node


def schema_of(model_cls: type[BaseModel]) -> dict[str, Any]:
    """pydantic 模型 → OpenAI strict json_schema。`$defs` / `$ref` 保留（strict 支援）。"""
    return _strictify(model_cls.model_json_schema())


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return defs.get(ref.split("/")[-1], {})
    return node


def _strip_nulls(data: Any, node: dict[str, Any], defs: dict[str, Any]) -> Any:
    """strict 模式下模型會對「原本可省略」的欄位回 null；pydantic 遇到非 Optional 欄位的 null
    會驗證失敗。依**原始** schema（strict 化之前）把這類 null 刪掉，讓 pydantic 套預設值。"""
    node = _resolve(node, defs)
    if isinstance(data, dict):
        props = node.get("properties") or {}
        required = set(node.get("required") or [])
        for key in list(data):
            sub = props.get(key)
            if sub is None:
                continue
            sub_resolved = _resolve(sub, defs)
            if data[key] is None and key not in required and not _is_nullable(sub_resolved):
                del data[key]
            else:
                data[key] = _strip_nulls(data[key], sub, defs)
        return data
    if isinstance(data, list) and isinstance(node.get("items"), dict):
        return [_strip_nulls(item, node["items"], defs) for item in data]
    if data is not None and node.get("anyOf"):
        branches = [b for b in node["anyOf"] if isinstance(b, dict) and b.get("type") != "null"]
        if len(branches) == 1:
            return _strip_nulls(data, branches[0], defs)
    return data


# ── usage / 回應解析 ──────────────────────────────────────────────────────

def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage_of(raw: Any) -> dict[str, int]:
    """OpenAI usage → 計費欄位。`input_tokens` 是**總數**（含 cached 與 write）。

    欄位一律容錯讀取：SDK 版本間欄位名有變動風險，缺欄位當 0，不讓晨報因遙測失敗。
    """
    in_details = _get(raw, "input_tokens_details") or {}
    out_details = _get(raw, "output_tokens_details") or {}
    return {
        "input_tokens": int(_get(raw, "input_tokens", 0) or 0),
        "output_tokens": int(_get(raw, "output_tokens", 0) or 0),
        "cached_tokens": int(_get(in_details, "cached_tokens", 0) or 0),
        "cache_write_tokens": int(_get(in_details, "cache_write_tokens", 0) or 0),
        "reasoning_tokens": int(_get(out_details, "reasoning_tokens", 0) or 0),
        "tool_tokens": 0,
    }


def _merge_usage(acc: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    for key, value in new.items():
        acc[key] = acc.get(key, 0) + value
    return acc


def _text_of(output: Any) -> str:
    parts: list[str] = []
    for item in output or []:
        if _get(item, "type") != "message":
            continue
        for content in _get(item, "content") or []:
            if _get(content, "type") == "output_text":
                parts.append(str(_get(content, "text") or ""))
    return "".join(parts).strip()


def _refusal_of(output: Any) -> str | None:
    for item in output or []:
        if _get(item, "type") != "message":
            continue
        for content in _get(item, "content") or []:
            if _get(content, "type") == "refusal":
                return str(_get(content, "refusal") or "") or "（未提供原因）"
    return None


def _as_input(item: Any) -> Any:
    """把上一輪的 output item 原樣接回 input。SDK 物件轉 dict；測試的 SimpleNamespace 原樣傳。"""
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    return item


def _map_error(exc: Exception) -> LLMError:
    """SDK 例外 → 供應商中立例外。上層降級邏輯不認得任何 openai 型別（FR-014）。"""
    import openai

    if isinstance(exc, openai.RateLimitError):          # 含 insufficient_quota
        return LLMQuotaExceeded(f"OpenAI 429 配額用盡: {exc}")
    if isinstance(exc, openai.BadRequestError):
        return LLMBadRequest(f"OpenAI 400 請求被拒: {exc}")
    if isinstance(exc, openai.APIConnectionError):      # 含 APITimeoutError
        return LLMUnavailable(f"OpenAI 連線失敗或逾時: {exc}")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return LLMUnavailable(f"OpenAI {exc.status_code} 暫時不可用: {exc}")
        return LLMBadRequest(f"OpenAI HTTP {exc.status_code}: {exc}")
    return LLMError(f"OpenAI 呼叫失敗: {exc}")


def _with_attempts(
    exc: LLMError, attempts: list[FetchAttempt], usage: dict[str, int] | None = None,
) -> LLMError:
    """把已發生的查證行為（與已花掉的 usage）掛在例外上（同 claude_client._with_attempts）。

    降級處（`morning_brief._decide_brief`）會接住它——失敗那次的查證是真的發生過，
    不能連同 usage 一起從遙測裡消失。`usage` 讓影子對照在失敗時也能照實記帳：
    跑了三輪才倒下，那三輪的錢已經花了。
    """
    exc.fetch_attempts = attempts  # type: ignore[attr-defined]
    exc.usage = dict(usage or {})  # type: ignore[attr-defined]
    if attempts:
        logger.warning(
            "OpenAI 呼叫以例外結束，但期間已發生 %d 次 web_fetch（成功 %d）",
            len(attempts), sum(1 for a in attempts if a.ok),
        )
    return exc


def _function_output(call: Any, session: FetchSession | None) -> str:
    name = _get(call, "name")
    if name != "web_fetch" or session is None:
        return json.dumps({"ok": False, "error_code": "unknown_tool",
                           "message": f"沒有名為 {name} 的工具"}, ensure_ascii=False)
    try:
        args = json.loads(_get(call, "arguments") or "{}")
        url = str(args.get("url") or "") if isinstance(args, dict) else ""
    except (TypeError, json.JSONDecodeError):
        url = ""
    output, _attempt = session.fetch(url)
    return output


# ── 主迴圈 ────────────────────────────────────────────────────────────────

def _run(
    *,
    model: str,
    instructions: str,
    user_prompt: str,
    tools: list[dict[str, Any]],
    effort: str,
    text_format: dict[str, Any] | None = None,
    fetch_session: FetchSession | None = None,
    search_limit: int = 0,
) -> tuple[Any, dict[str, int]]:
    """跑一次完整工具迴圈，回 (最終 response, 累計 usage)。失敗 raise 中立例外（帶 attempts）。"""
    attempts = (lambda: fetch_session.attempts if fetch_session else [])
    try:
        client = _client()
    except LLMError as exc:
        raise _with_attempts(exc, []) from exc

    params: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "reasoning": {"effort": effort},
        "max_output_tokens": int(settings.openai_max_output_tokens),
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if tools:
        params["tools"] = tools
        # 一次一個：額度計數單純，且 fetch 結果能影響下一個要開什麼
        params["parallel_tool_calls"] = False
    if text_format is not None:
        params["text"] = {"format": text_format}
    has_search = any(t.get("type") == "web_search" for t in tools)
    has_fetch = any(t.get("name") == "web_fetch" for t in tools)

    items: list[Any] = [{"role": "user", "content": user_prompt}]
    usage: dict[str, int] = {}
    searches = 0
    max_rounds = max(int(settings.openai_max_tool_rounds), 1)

    def _stamp(rounds: int) -> None:
        usage["rounds"] = rounds
        usage["web_search_requests"] = searches
        usage["web_fetch_requests"] = len(attempts())

    for round_idx in range(max_rounds):
        call_params = dict(params)
        if has_search:
            remaining = max(int(search_limit) - searches, 0)
            if remaining > 0:
                # max_tool_calls 是**單一 response** 內 built-in 工具的上限；跨輪要自己扣
                call_params["max_tool_calls"] = remaining
            else:
                # 搜尋額度用完：用 allowed_tools 限縮而**不改 tools 清單**——改清單會打掉快取前綴
                call_params["tool_choice"] = {
                    "type": "allowed_tools", "mode": "auto",
                    "tools": [{"type": "function", "name": "web_fetch"}] if has_fetch else [],
                }
                if not has_fetch:
                    call_params["tool_choice"] = "none"
        try:
            response = client.responses.create(**call_params, input=items)
        except Exception as exc:  # noqa: BLE001 — 統一映射成中立例外
            _stamp(round_idx)
            raise _with_attempts(_map_error(exc), attempts(), usage) from exc
        monitor.mark("ai")

        _merge_usage(usage, _usage_of(_get(response, "usage")))
        output = list(_get(response, "output") or [])
        searches += sum(1 for o in output if _get(o, "type") == "web_search_call")

        status = _get(response, "status")
        if status == "incomplete":
            reason = _get(_get(response, "incomplete_details"), "reason")
            _stamp(round_idx + 1)
            # reasoning 吃光 max_output_tokens 時回的是半份——不得當成功晨報發布
            raise _with_attempts(
                LLMError(f"OpenAI 回應未完成（reason={reason}），放棄這次輸出"), attempts(), usage,
            )
        if status == "failed":
            err = _get(response, "error")
            _stamp(round_idx + 1)
            raise _with_attempts(
                LLMUnavailable(f"OpenAI 回應失敗: {_get(err, 'message') or err}"), attempts(), usage,
            )
        refusal = _refusal_of(output)
        if refusal is not None:
            _stamp(round_idx + 1)
            raise _with_attempts(LLMRefused(f"OpenAI 婉拒此請求：{refusal}"), attempts(), usage)

        calls = [o for o in output if _get(o, "type") == "function_call"]
        if not calls:
            _stamp(round_idx + 1)
            return response, usage

        items.extend(_as_input(o) for o in output)
        for call in calls:
            items.append({
                "type": "function_call_output",
                "call_id": _get(call, "call_id"),
                "output": _function_output(call, fetch_session),
            })

    _stamp(max_rounds)
    raise _with_attempts(
        LLMError(
            f"OpenAI 工具迴圈跑滿 {max_rounds} 輪仍未產出最終答案，放棄"
            "（可調高 OPENAI_MAX_TOOL_ROUNDS 或調低查證/搜尋上限）"
        ),
        attempts(), usage,
    )


def generate_structured(
    model_cls: type[BaseModel],
    *,
    system: str,
    user_prompt: str,
    model: str,
    tools: list[dict[str, Any]],
    effort: str,
    fetch_session: FetchSession | None = None,
    search_limit: int = 0,
) -> tuple[BaseModel, dict[str, int], list[FetchAttempt], str | None]:
    """帶查證工具跑一次並驗成指定 pydantic 模型。

    回 (parsed, usage, 查證 attempts, 回應中的實際模型版本)。
    """
    original = model_cls.model_json_schema()
    text_format = {
        "type": "json_schema",
        "name": model_cls.__name__,
        "schema": schema_of(model_cls),
        "strict": True,
    }
    response, usage = _run(
        model=model, instructions=system, user_prompt=user_prompt, tools=tools,
        effort=effort, text_format=text_format, fetch_session=fetch_session,
        search_limit=search_limit,
    )
    attempts = fetch_session.attempts if fetch_session else []
    version = _get(response, "model")
    text = _text_of(_get(response, "output"))
    if not text:
        raise _with_attempts(LLMError("OpenAI 回空內容（缺 output_text）"), attempts, usage)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _with_attempts(
            LLMError(f"OpenAI 回的不是合法 JSON: {exc}; head={text[:200]}"), attempts, usage,
        ) from exc
    payload = _strip_nulls(payload, original, original.get("$defs") or {})
    try:
        parsed = model_cls.model_validate(payload)
    except ValidationError as exc:
        raise _with_attempts(
            LLMError(f"OpenAI 輸出不符 {model_cls.__name__} schema: {exc}"), attempts, usage,
        ) from exc
    return parsed, usage, attempts, (str(version) if version else None)


def generate_text(
    *,
    system: str,
    user_prompt: str,
    model: str,
    effort: str,
    search_limit: int,
) -> tuple[str, dict[str, int]]:
    """純文字作答（Discord 問答）。只給 hosted web_search：問答沒有 facts pack，
    給 web_fetch 也只會全部被白名單拒絕。"""
    tools = build_tools(fetch=False, search=search_limit > 0)
    response, usage = _run(
        model=model, instructions=system, user_prompt=user_prompt, tools=tools,
        effort=effort, search_limit=search_limit,
    )
    return _text_of(_get(response, "output")), usage
