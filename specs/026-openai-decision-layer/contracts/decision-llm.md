# Contract: 決策層與查證工具

## 1. DecisionLLM（`backend/ai/llm_client.py`，Protocol 不變）

```python
class OpenAIDecisionLLM:
    name = "openai"
    def draft_brief(features, facts_json, calibration, *, frugal=False)
        -> tuple[BriefDraft, Usage, list[FetchAttempt]]
    def answer_question(system_prompt, user_prompt, *, cacheable) -> tuple[str, Usage]
```

- `draft_brief`：
  - `frugal=False`：tools = [`web_fetch`(function), `web_search`(hosted)]，白名單＝`facts_json` 內所有 `url`。
  - `frugal=True`：tools = []、facts 傳 `"[]"`、effort=`low`（與 Anthropic 實作同語意）。
  - system / user prompt 用 `prompts.build_decision_system(verify=not frugal)` / `build_decision_prompt(...)`，**與 Claude 完全相同字串**。
  - 失敗一律 raise `ai.errors` 的中立例外，且 `exc.fetch_attempts` 帶已發生的查證行為（同 `claude_client._with_attempts`）。
- `answer_question`：tools = [`web_search`]；`cacheable` 無作用（OpenAI 自動快取）。
- `get_decision_llm("openai")` 回此實作；未知名稱維持 `ValueError`。

## 2. web_fetch 工具（宣告給模型，順序固定：web_fetch 在前、web_search 在後）

```json
{
  "type": "function",
  "name": "web_fetch",
  "description": "開啟 facts pack 中列出的來源網址並回傳原文純文字。只能開啟 facts pack 內的 url。",
  "strict": true,
  "parameters": {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "facts pack 中某則 event 的 url"}},
    "required": ["url"],
    "additionalProperties": false
  }
}
```

```json
{"type": "web_search"}
```

### 回給模型的 `function_call_output.output`（JSON 字串）

成功：
```json
{"ok": true, "url": "<請求網址>", "final_url": "<實際網址>", "retrieved_at": "ISO8601", "truncated": true, "content": "<純文字>"}
```
失敗：
```json
{"ok": false, "url": "<請求網址>", "error_code": "url_not_allowed | max_uses_exceeded | url_not_accessible | unavailable | unsupported_content_type", "message": "<給模型看的一句話>"}
```

## 3. fetch_tool 模組（`backend/ai/fetch_tool.py`）

```python
class FetchSession:
    def __init__(self, allowed_urls: Iterable[str], *, max_uses: int, max_chars: int): ...
    def fetch(self, url: str) -> tuple[str, FetchAttempt | None]:
        """回 (給模型的 JSON 字串, 新增的 attempt；重複 URL 回 None)。永不 raise。"""
    @property
    def attempts(self) -> list[FetchAttempt]: ...
```

不變式：
- 白名單外、非 http(s)、解析到非公開 IP 的請求**不發出任何網路連線**。
- 每一跳轉址都重新檢查 scheme 與 IP。
- `len(attempts) <= max_uses + 拒絕次數`；成功＋網路失敗的次數 ≤ `max_uses`。
- 任何例外都轉成失敗 attempt，不讓晨報掛掉。

## 4. 錯誤映射（`openai_client._map_error`）

見 research R8。上層（`morning_brief._decide_brief`、`api/ask.py`）只認 `LLMError` 家族。
