"""Claude 決策 + 查證層（spec 022-llm-tiering）。

與 Gemini 路徑刻意不同：這裡走**官方 anthropic SDK**（Gemini 走 raw REST 是歷史因素）。
SDK 內建重試、streaming、structured outputs，自己刻沒有價值。

本層的職責是「決策 + 查證」，不是「搜尋」：
- 吃 features JSON（本地 parquet，零 LLM）+ facts pack（Gemini 召回的**待查證線索**）+ 回測校準
- 用 `web_fetch` 逐條開啟 facts pack 引用的 URL 核對後才採信；`web_search` 只用於補漏
- 一次呼叫直接產出結構化結果（Anthropic 無「schema 不能與 tools 並用」的限制，
  故 Gemini 路徑那個純格式化的第②段在這裡完全不需要）

## 幾個一定會踩到的坑（改動前先讀）

1. **不可帶 `temperature` / `top_p` / `top_k`** — Opus 5 已移除這些參數，帶了直接 400。
   Gemini 路徑的 `temperature=0.4/0.1` 不可平移過來。
2. **不可用 assistant prefill** — 同樣 400。結構化輸出一律走 `output_config.format`。
3. **必須 streaming** — `max_tokens` 遠大於 16k 時非 streaming 會撞 SDK HTTP timeout，
   SDK 會直接 raise ValueError。
4. **必須處理 `pause_turn`** — server-side 工具（web_fetch/web_search）跑到內部迭代上限會
   回 `stop_reason="pause_turn"`。**SDK 不會自動續跑**，漏處理會拿到一份靜默截斷的晨報：
   沒有錯誤、沒有警告，只是內容少一半。
5. **`web_fetch` 不可開 `citations`** — 與 `output_config.format` 互斥，同開直接 400。
   來源追蹤走我們自己的 `fact_checks` 欄位。
6. **不可另外宣告 `code_execution`** — `_20260209` 版工具內建 dynamic filtering
   （底層自己會跑 code execution），重複宣告會造成兩個執行環境、讓模型混亂。
7. **不要再包 tenacity** — SDK 自己有 `max_retries`。`tests/test_gemini_retry.py` 就是在防
   「3×4=12 次」的重複重試 bug，同一個坑不踩第二次。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
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

logger = logging.getLogger("ai-market-backend.claude")

# SDK 內建重試（429/5xx/連線錯誤，指數退避）。設 2 次＝總共 3 次嘗試，與 Gemini 路徑的
# stop_after_attempt(4) 量級相當。**不要**再往外包一層 tenacity。
_MAX_RETRIES = 2
# 晨報 input 大、thinking 深，單次可能跑數分鐘；SDK 預設 10 分鐘，這裡給足。
_TIMEOUT_SECONDS = 900.0

_client_singleton: anthropic.Anthropic | None = None


def _client() -> anthropic.Anthropic:
    global _client_singleton
    if not settings.anthropic_api_key.strip():
        raise LLMError("ANTHROPIC_API_KEY 未設定")
    if _client_singleton is None:
        _client_singleton = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            max_retries=_MAX_RETRIES,
            timeout=_TIMEOUT_SECONDS,
        )
    return _client_singleton


def reset_client() -> None:
    """測試用：清掉 singleton，讓下次呼叫依當前 settings 重建。"""
    global _client_singleton
    _client_singleton = None


def build_tools(fetch_uses: int, search_uses: int) -> list[dict[str, Any]]:
    """組出連網查證工具清單。

    `max_uses` 是**成本閘門**（憲章 II）：沒有上限等於把單次呼叫的成本上界交給模型自由心證。
    但也不能調太低——模型會在查證到一半被切斷，產出「部分查證」的結果，比不查證更糟，
    因為它看起來像查過了。

    ⚠️ 清單內容與**順序**必須跨請求逐字穩定：tools 渲染在 prompt 最前面
    （`tools` → `system` → `messages`），任何增減或換序都會讓整個 prompt cache 前綴失效。
    """
    return [
        # web_fetch 只能抓取**已出現在對話裡的 URL**——這個限制正是本設計的核心：
        # 模型只能去開 facts pack 真的引用過的連結，無法自己生一個 URL 出來，
        # 天然形成稽核閉環。citations 保持關閉（與 output_config.format 互斥）。
        {
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": int(fetch_uses),
            "max_content_tokens": 8000,  # 防單頁爆量（一頁新聞可能數千 token）
        },
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": int(search_uses),
        },
    ]


def _strictify(node: Any) -> Any:
    """把 pydantic 產的 JSON Schema 就地補成 structured outputs 要求的嚴格形式。

    每個 object 節點都要 `additionalProperties: false`。pydantic 不會加，缺了會被 API 拒。
    """
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for value in node.values():
            _strictify(value)
    elif isinstance(node, list):
        for item in node:
            _strictify(item)
    return node


def schema_of(model: type[BaseModel]) -> dict[str, Any]:
    """pydantic 模型 → structured outputs 用的 JSON Schema。

    刻意**不**手寫第二套 schema：現行 `GEMINI_BRIEF_SCHEMA` 用的 `"nullable": True` 是
    Google OpenAPI 方言，不是合法的 JSON Schema draft-2020，平移過來會被拒。
    """
    return _strictify(model.model_json_schema())


def _usage_of(raw: Any) -> dict[str, int]:
    """把 Anthropic usage 正規化成計費欄位。

    ⚠️ **與 Gemini 語意相反**：Gemini 的 `promptTokenCount` **已含** cached tokens，
    Anthropic 的 `input_tokens` **不含**。`cost.tracker` 必須依供應商分流，
    不可共用 `input - cached` 那套減法（會重複扣）。
    """
    return {
        "input_tokens": int(getattr(raw, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(raw, "output_tokens", 0) or 0),
        "cached_tokens": int(getattr(raw, "cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
        "tool_tokens": 0,  # Anthropic 不分開回報；server tool 的 token 已計入 input/output
    }


def _merge_usage(acc: dict[str, int], new: dict[str, int]) -> dict[str, int]:
    for key, value in new.items():
        acc[key] = acc.get(key, 0) + value
    return acc


def _count_web_searches(content: Any) -> int:
    """數這一輪用掉幾次 web_search（server tool 按次計費）。

    刻意從 content blocks 自己數，而不是讀 usage 上某個欄位——後者的欄位名在 SDK 版本間
    有變動風險，數 block 是穩定的。
    """
    total = 0
    for block in content or []:
        if getattr(block, "type", None) == "server_tool_use" and \
                getattr(block, "name", None) == "web_search":
            total += 1
    return total


def _text_of(content: Any) -> str:
    return "".join(
        getattr(b, "text", "") for b in content or [] if getattr(b, "type", None) == "text"
    ).strip()


def _map_error(exc: Exception) -> LLMError:
    """把 SDK 例外映射成供應商中立例外，讓上層降級邏輯不必認得 anthropic 型別。"""
    if isinstance(exc, anthropic.RateLimitError):
        return LLMQuotaExceeded(f"Claude 429 配額用盡: {exc}")
    if isinstance(exc, anthropic.BadRequestError):
        return LLMBadRequest(f"Claude 400 請求被拒: {exc}")
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMUnavailable(f"Claude 連線失敗: {exc}")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return LLMUnavailable(f"Claude {exc.status_code} 暫時不可用: {exc}")
        return LLMBadRequest(f"Claude HTTP {exc.status_code}: {exc}")
    return LLMError(f"Claude 呼叫失敗: {exc}")


def _run(
    *,
    model: str,
    system: list[dict[str, Any]] | str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    output_schema: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, int]]:
    """跑一次完整對話（含 pause_turn 續跑），回 (最終 message, 累計 usage)。

    **pause_turn 是這裡最重要的邏輯**：server-side 工具跑到內部迭代上限時，API 會回
    `stop_reason="pause_turn"` 和一段**未完成**的內容。必須把該 assistant turn 接回
    messages 再送一次，模型才會從中斷處續跑。漏掉這段不會有任何錯誤訊號——
    只會拿到一份內容少一半的晨報。
    """
    client = _client()
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": int(settings.claude_max_tokens),
        # adaptive：讓模型自己決定要想多久。**不可**帶 temperature/top_p/top_k（Opus 5 會 400）。
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": settings.claude_effort},
        "system": system,
        "tools": tools,
    }
    if output_schema is not None:
        params["output_config"]["format"] = {
            "type": "json_schema",
            "schema": output_schema,
        }

    convo = list(messages)
    usage: dict[str, int] = {}
    searches = 0
    max_rounds = int(settings.claude_max_continuations) + 1

    for round_idx in range(max_rounds):
        try:
            with client.messages.stream(**params, messages=convo) as stream:
                message = stream.get_final_message()
        except Exception as exc:  # noqa: BLE001 — 統一映射成中立例外
            raise _map_error(exc) from exc
        monitor.mark("ai")  # 記一次對外 AI 流量（待機偵測用）

        _merge_usage(usage, _usage_of(getattr(message, "usage", None)))
        searches += _count_web_searches(message.content)

        stop = getattr(message, "stop_reason", None)
        # refusal 是 HTTP 200 + stop_reason，不是錯誤回應。必須在讀 content 之前檢查——
        # 直接讀 content[0] 會在 refusal 時炸掉（content 可能是空陣列）。
        if stop == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            usage["web_search_requests"] = searches
            raise LLMRefused(f"Claude 婉拒此請求（category={category}）")
        if stop == "pause_turn":
            logger.info("Claude pause_turn，續跑第 %d/%d 輪", round_idx + 1, max_rounds - 1)
            convo = convo + [{"role": "assistant", "content": message.content}]
            continue

        usage["web_search_requests"] = searches
        return message, usage

    usage["web_search_requests"] = searches
    raise LLMError(
        f"Claude 連續 {max_rounds} 輪都回 pause_turn 仍未完成，放棄"
        "（避免無窮迴圈；可調高 CLAUDE_MAX_CONTINUATIONS 或調低 max_uses）"
    )


def generate_structured(
    model_cls: type[BaseModel],
    *,
    system: str,
    user_prompt: str,
    model: str,
    tools: list[dict[str, Any]],
) -> tuple[BaseModel, dict[str, int]]:
    """帶連網查證工具跑一次，並把結果驗成指定 pydantic 模型。"""
    message, usage = _run(
        model=model,
        system=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": user_prompt}],
        tools=tools,
        output_schema=schema_of(model_cls),
    )
    text = _text_of(message.content)
    if not text:
        raise LLMError("Claude 回空內容（structured output 缺 text block）")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Claude 回的不是合法 JSON: {exc}; head={text[:200]}") from exc
    try:
        return model_cls.model_validate(payload), usage
    except ValidationError as exc:
        raise LLMError(f"Claude 輸出不符 {model_cls.__name__} schema: {exc}") from exc


def generate_answer(
    *,
    system_blocks: list[dict[str, Any]],
    user_prompt: str,
    model: str,
    tools: list[dict[str, Any]],
) -> tuple[str, dict[str, int]]:
    """純文字作答（Discord 問答）。system_blocks 允許帶 cache_control。"""
    message, usage = _run(
        model=model,
        system=system_blocks,
        messages=[{"role": "user", "content": user_prompt}],
        tools=tools,
    )
    return _text_of(message.content), usage
