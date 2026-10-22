# Data Model: spec 026

本案不新增資料庫表，只擴充設定、報告 JSON 與 redis 成本桶。

## 1. 設定（`backend/config.py` / `.env`）

| 欄位 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `llm_decision_provider` | str | `anthropic` | 值域擴為 `anthropic` / `openai` / `gemini`；未知值 fail-fast（現行行為） |
| `openai_api_key` | str | `""` | 未設定時系統正常啟動；選 openai 時走 Gemini 降級並標示原因 |
| `openai_model_decision` | str | `gpt-6.1-sol` | 晨報決策 |
| `openai_model_chat` | str | `gpt-6.1-sol` | 問答 |
| `openai_brief_effort` | str | `medium` | 晨報推理強度；節儉模式強制 `low` |
| `openai_effort` | str | `medium` | 問答推理強度 |
| `openai_max_output_tokens` | int | `32000` | reasoning + 回答合計上限 |
| `openai_brief_fetch_uses` | int | `3` | 每篇查證次數上限（對齊 `claude_brief_fetch_uses`） |
| `openai_brief_search_uses` | int | `2` | 每篇補漏搜尋上限（`max_tool_calls` 與 prompt 雙重約束） |
| `openai_chat_search_uses` | int | `2` | 問答搜尋上限 |
| `openai_fetch_max_chars` | int | `12000` | 單頁回給模型的純文字上限 |
| `openai_max_tool_rounds` | int | `8` | 工具迴圈總輪數上限 |
| `openai_shadow_until` | date \| None | `None` | 影子對照到期日（含當日）；`None`＝關閉 |
| `openai_shadow_cost_estimate_twd` | float | `15.0` | 跑影子前的預算預留額 |

⚠️ `.env` 不可寫行內 `#` 註解（docker env_file 不剝除，pydantic 解 int/bool/date 會炸）。

## 2. FetchAttempt（`backend/ai/claude_client.py` 既有 dataclass，擴充）

沿用既有欄位：`url`、`ok`、`error_code`、`retrieved_at`、`content_chars`、`via_code`。

新增（選填，預設 `None`，舊報告與 Claude 路徑不受影響）：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `reject_reason` | str \| None | `not_in_facts_pack` / `private_address` / `bad_scheme`；只有 `error_code=url_not_allowed` 時有值 |
| `final_url` | str \| None | 跟完轉址後的實際網址（與 `url` 不同時才記） |

建議把 dataclass 搬到中立模組（如 `backend/ai/fetch_tool.py` 或 `ai/types.py`），`claude_client` 改為 re-export，避免 openai 路徑 import claude_client。

**狀態規則**：
- 白名單外 / 私有位址 / 非 http(s) → `ok=False, error_code="url_not_allowed"`，不發任何網路請求。
- 已達上限 → `ok=False, error_code="max_uses_exceeded"`。
- 同一 URL 重複請求 → 不新增 attempt、不扣額度，回快取內容。
- HTTP ≥400 / 連線錯誤 → `url_not_accessible`；逾時 → `unavailable`；非 text/html、text/plain → `unsupported_content_type`。
- 成功 → `ok=True, content_chars=len(純文字截斷前)`。

## 3. Usage dict（各 client 正規化後，`cost.tracker` 吃的格式）

OpenAI 版 `_usage_of` 產出：

| 鍵 | 來源 | 語意 |
|---|---|---|
| `input_tokens` | `usage.input_tokens` | **總** input（含 cached 與 write） |
| `cached_tokens` | `input_tokens_details.cached_tokens` | cache read，不含 write |
| `cache_write_tokens` | `input_tokens_details.cache_write_tokens` | cache write |
| `output_tokens` | `usage.output_tokens` | 含 reasoning |
| `reasoning_tokens` | `output_tokens_details.reasoning_tokens` | 遙測用，不另計價 |
| `web_search_requests` | 數 output 中 `web_search_call` items | 按次計價 |
| `web_fetch_requests` | 本地 fetch_tool 實際執行次數 | 遙測 |
| `rounds` | 迴圈輪數 | 遙測 |
| `cache_ttl` | 固定 `"30m"` | 計價用 |

多輪呼叫逐輪加總。

## 4. 成本計價（`cost/tracker.py`）

```
billable_uncached = max(input_tokens - cached_tokens - cache_write_tokens, 0)
usd = billable_uncached × in
    + cached_tokens × cached
    + cache_write_tokens × write
    + output_tokens × out
    + web_search_requests × 0.01
TWD = usd × 32
```

`provider_of(model)`：`claude-` → anthropic；`gpt-` / `o` 系列 → openai；其餘 → gemini。`PROVIDERS` 擴為 `("gemini", "anthropic", "openai")`。

## 5. 報告 JSON

### 主報告（`REPORTS_DIR/morning_*.json`）
- `cost.decision_provider` 值域新增 `openai`；`decision_model` 欄位新增（實際請求的 model 名）、`decision_model_version`（回應中的 model）。
- `cost.tokens` 新增 `reasoning_tokens`（Claude 路徑為 0）。
- 新增 `shadow`：`{"status": "ok" | "error" | "skipped", "reason": str | None, "path": str | None}`——只是指標，影子內容不進主報告。影子關閉（`OPENAI_SHADOW_UNTIL` 未設）時**不加**此欄位，主報告格式與導入前相同。
- 新增 `cost.decision_output_tokens`：決策層自己的 output（`tokens` 是召回＋決策合計），供 provider_compare 公平比較輸出量。
- 新增 `cost.decision_fallback_reason`（只在降級時出現）：主路徑倒下的原因，例如 `openai: OPENAI_API_KEY 未設定`；key 未設這類設定問題會顯示在降級提示上。

### 影子報告（`REPORTS_DIR/shadow/<report_id>_openai.json`）
```
{
  "report_id": "<主報告 id>",
  "report_date": "YYYY-MM-DD",
  "shadow_of": "<主報告 id>",
  "decision_provider": "openai",        # 影子失敗不跑 Gemini 降級，只寫 error（2026-10-22 決定）
  "decision_model": "...", "decision_model_version": "...",
  "cost": {"decision_provider": "openai", "decision_model": "...", "frugal_mode": false,
           "decision_twd": float, "retrieval_twd": float,   # 召回共用主報告的值，比較時兩邊加同一筆
           "tokens": {...同主報告格式}, "verification": {...}},  # 形狀對齊主報告 → verification_stats 可直接讀
  "facts": [...],             # 與主報告相同（共用）
  "fact_checks": [...],
  "verification": {...},      # `_verification_stats` 同格式 → verification_stats CLI 可直接讀
  "guardrail": {...},         # run_guardrails 結果（error_count 用於比較）
  "result": {...},            # BriefResult（guardrail 後）
  "markdown": "...",          # 供人工並排閱讀
  "error": str | None
}
```
檔名不以 `morning_` 開頭且位於子目錄，任何現行 glob 都撿不到。

## 6. Redis 成本桶
- 新增 `cost:month:YYYYMM:openai`（與既有 per-provider 桶同 TTL 70d）。
- 彙總日／月桶不變——影子花費一樣寫入，預算閘門看得到。
