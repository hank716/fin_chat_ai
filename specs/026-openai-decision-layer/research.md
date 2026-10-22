# Research: 決策層新增 OpenAI 供應商（spec 026）

資料來源：OpenAI 官方文件（developers.openai.com，2026-10-19 / 10-21 讀取）、現行程式碼、10 月 10 篇晨報實測。

## R1. 呼叫介面：Responses API + 官方 SDK

- **Decision**：用官方 `openai` SDK（容器內可裝最新 3.35.1，pin `openai>=3.35,<3.36`）的 `client.responses.create`，非 streaming（或 SDK 預設 timeout 拉長到 900s）。
- **Rationale**：與 `claude_client` 同一個理由——SDK 內建重試（`max_retries`）、型別、錯誤類別。Responses API 才同時支援 function tools、hosted `web_search`、`text.format` json_schema、`reasoning.effort`。Chat Completions 的 web search 只限 `gpt-5-search-api` 這類專用模型。
- **Alternatives**：raw REST（Gemini 那樣）——沒必要自己刻重試與錯誤映射；Chat Completions——不支援所需的工具組合。

## R2. 結構化輸出 schema

- **Decision**：新增 `openai_client.schema_of()`：從 pydantic `model_json_schema()` 出發，每個 object 補 `additionalProperties: false`，並把**所有** property 列進 `required`；原本有預設值、非必填的欄位改成 `anyOf [..., {"type":"null"}]`（或 type 加 `"null"`）。`$defs` / `$ref` 保留（strict 模式支援）。
- **Rationale**：OpenAI strict 模式規則：「All fields in properties must be listed in required；optional 以加 null 表示」。直接沿用 `claude_client._strictify` 會因 required 不全被拒。模型回 `null` 後，pydantic 驗證時對有預設值的欄位要把 `None` 視同缺省——在 `generate_structured` 驗證前先把值為 `None` 且原 schema 非 nullable 的鍵刪掉，再 `model_validate`。
- **Alternatives**：`strict: false`——不保證符合 schema，退化成「parse 失敗就降級」，晨報失敗率上升，拒絕。手寫第二套 schema——與 `BriefDraft` 雙軌維護，拒絕。
- **驗證**：單元測試對 `BriefDraft` 產出的 schema 斷言「每個 object 的 required == properties 的 keys」且 `additionalProperties is False`。

## R3. 查證工具：本地 function tool，名稱沿用 `web_fetch`

- **Decision**：宣告一個 `type:"function"`、`name:"web_fetch"`、`strict:true`、參數 `{url: string}` 的工具；呼叫在本地執行（新模組 `backend/ai/fetch_tool.py`）。補漏搜尋用 hosted `{"type":"web_search"}`。**工具名刻意與 Claude 相同**，`prompts.build_decision_system/prompt` 不需改一字。
- **Rationale**：
  1. OpenAI 沒有「只能開對話中出現過的 URL」的抓頁工具（`web_search` 的 `open_page` 是模型自主動作，不能指定 URL、也無法限制）。
  2. 本地執行讓白名單變成**程式強制**而非供應商政策：只允許當篇 facts pack 的 URL，比 Anthropic 的 `url_not_in_prior_context` 更嚴（後者允許模型開 web_search 結果裡的 URL）。
  3. A/B 公平性：兩邊吃同一份 prompt，差異只剩模型本身。
- **白名單比對**：正規化後比對（小寫 scheme/host、去掉 fragment、去尾 `/`）；不做前綴匹配。
- **SSRF 防護**：解析 host 的所有 IP，任何一個落在 private / loopback / link-local / reserved / multicast 即拒絕；只允許 http/https；redirect 手動跟（最多 5 跳），**每一跳**重新檢查 IP 與 scheme（跳轉到白名單外的公開網址允許——新聞站常有 canonical 轉址——但仍記錄最終網址）。
- **內容處理**：timeout 10s、最多下載 2 MB；`text/html` 用 BeautifulSoup（`html.parser`，已在容器內，requirements 補明確 pin）去掉 script/style/nav/footer 取純文字；`text/plain` 原樣；其他（PDF、圖片）回「unsupported_content_type」不傳內容。截斷到 `openai_fetch_max_chars`（預設 12,000 字元 ≈ 3k token，對齊 Claude 現行 `max_content_tokens` 量級）。
- **額度**：同一 URL 第二次請求回上次結果、不扣額度；超過 `openai_brief_fetch_uses` 回 `max_uses_exceeded`。
- **錯誤碼**：沿用 spec 023 既有值域，讓 `verification_stats.classify_outcomes` 不必改：`max_uses_exceeded`、`url_not_allowed`（白名單外／私有位址）、`url_not_accessible`（HTTP≥400、連線失敗）、`unsupported_content_type`、`unavailable`（逾時）。已核對 `verification_stats.py:55-59`：這五個碼全部落在既有的 `_BUDGET_CODES` / `_UNREACHABLE_CODES` / `_TRANSIENT_CODES`，分類邏輯**零改動**。「白名單拒絕」與「私有位址拒絕」都用 `url_not_allowed`（歸「來源打不開」），區別寫在 `FetchAttempt` 新增的選填欄位 `reject_reason`（`not_in_facts_pack` / `private_address`）供除錯，不影響統計。
- **連帶修正**：`morning_brief._verification_stats` 的 `fetch_limit` 目前寫死讀 `settings.claude_brief_fetch_uses`，要改成依 provider 取對應上限。
- **Alternatives**：付費抓頁服務（Jina Reader 等）——多一家供應商與成本，拒絕；讓模型直接用 web_search 查證——無法稽核開了什麼，違反憲章 I，拒絕。

## R4. 工具迴圈

- **Decision**：client 端迴圈：`responses.create` → 若 output 有 `function_call` 就逐一執行、把 `response.output`（含 reasoning items）整段接回 input，再附上 `function_call_output`；直到沒有 function_call。`parallel_tool_calls=False`（一次一個，額度計數單純）。總輪數上限 `openai_max_tool_rounds`（預設 8 = fetch 3 + search 輪次 + 餘裕），超過丟 `LLMError` 並帶 attempts。`store=False` + `include=["reasoning.encrypted_content"]`，不依賴伺服器端保存對話。
- **Rationale**：官方要求「reasoning items 必須跟 tool outputs 一起送回」。`store=False` 避免晨報內容被留在 OpenAI 端 30 天；encrypted reasoning 讓多輪推理不中斷。Anthropic 的 `pause_turn` 概念在這裡不存在——迴圈本來就在我們手上。
- **成本含意**：每一輪都重送累積對話，但 OpenAI 自動 prefix cache（GPT-5.6+ 最低 1,024 token）讓 ~58k 前綴從第 2 輪起以 cached 計（gpt-6.1-sol 為 0.05× input）。前綴穩定性要求同 Claude：tools 定義與順序固定、features JSON `sort_keys`、prompt 不含時間戳。
- **Alternatives**：`previous_response_id`——需 `store=True`，拒絕。

## R5. Prompt cache 設定

- **Decision**：不設 `prompt_cache_options`（用預設 30m TTL）；不設 `prompt_cache_key`（GPT-5.6+ 非必要）。
- **Rationale**：晨報單一請求內多輪重讀，30m 足夠；12h TTL 寫入 2.5×，每日一篇用不到。
- **計價（GPT-5.6+）**：每個 input token 只按一種費率計：未命中、cached read、cache write 三者互斥。欄位：`usage.input_tokens`（總數）、`usage.input_tokens_details.cached_tokens`（不含寫入）、`usage.input_tokens_details.cache_write_tokens`。→ **未命中 = input_tokens − cached − cache_write**。這與 Anthropic（input 不含 cache）與 Gemini（含 cached、無寫入欄）都不同，tracker 要第三條分支。
- 待 key 到手後第一篇實測核對：若 `input_tokens` 實際不含 write，公式改為 `input − cached`，對帳差距會立刻顯現（SC-004）。

## R6. 價目表（USD / 1M，2026-10-19 官方）

| model | input | cached | cache write(30m) | output |
|---|---|---|---|---|
| gpt-6.1-sol | 2.00 | 0.10 | 2.50 | 10.00 |
| gpt-6-sol | 2.00 | 0.20 | 2.50 | 10.00 |
| gpt-6-astra | 10.00 | 1.00 | 12.50 | 50.00 |
| gpt-5.6-sol | 4.00 | 0.40 | 5.00 | 20.00（優惠至 2026-11-21） |
| gpt-5.6-terra | 2.00 | 0.20 | 2.50 | 12.00 |
| gpt-6-luna | 0.10 | 0.01 | 0.125 | 0.50 |

- >272k input 有長文費率（晨報 ~60k 不會觸發；仍以 `_TIER_THRESHOLD_OPENAI=272_000` 實作，避免日後 features 變大時靜默低估）。
- web_search：$10 / 1k 次；搜尋內容 token 按 input 計（已含在 usage 內）。
- 未知 `gpt-*` → 以 gpt-6-astra 估（最貴，寧可高估），記 warning。
- reasoning token 已含在 `output_tokens`（`output_tokens_details.reasoning_tokens` 只做遙測）。
- 回應 `model` 欄位記實際版本，與價目表核對日期一併落地（比照 Gemini `PRICED_MODEL_VERSIONS`）。

## R7. 推理強度與輸出上限

- **Decision**：`reasoning={"effort": ...}`，晨報 `openai_brief_effort=medium`、問答 `openai_effort=medium`、節儉模式 `low`；`max_output_tokens=32000`（同 `claude_max_tokens`）。`status=="incomplete"` 且 `incomplete_details.reason=="max_output_tokens"` → `LLMError`（不得發布半份）。
- **Rationale**：與 Claude 現行設定對齊，A/B 才可比；effort 是輸出端的成本主閥（Claude 實測教訓）。

## R8. 錯誤映射

| OpenAI SDK | 中立例外 |
|---|---|
| `RateLimitError`（含 insufficient_quota） | `LLMQuotaExceeded` |
| `BadRequestError` | `LLMBadRequest` |
| `APIConnectionError` / `APITimeoutError` | `LLMUnavailable` |
| `APIStatusError` ≥500 | `LLMUnavailable` |
| 輸出含 `refusal` content | `LLMRefused` |
| key 未設定 | `LLMError("OPENAI_API_KEY 未設定")` → 走降級 |

## R9. 影子對照（US4 選項 A）

- **Decision**：在 `generate_morning_brief` **存檔並推送／發布主晨報之後**，若 `openai_shadow_until >= 今日` 且主晨報非節儉模式，以同一份 `feats`、`facts`、`calibration_text` 呼叫 `get_decision_llm("openai").draft_brief(...)`，套 `_backfill_caution` + `run_guardrails` + `_verification_stats`，結果寫到 `REPORTS_DIR/shadow/<report_id>_openai.json`。全程 try/except，失敗只寫 `{"error": ...}` 檔。
- **為何放在發布之後**：影子要多跑數分鐘，不能讓 08:30 晨報延遲；也確保影子失敗不可能影響主報告。
- **為何放子目錄**：`list_reports`、`latest_report_id`、`report_date_exists`、`verification_stats` 都 glob `REPORTS_DIR/morning_*.json`（非遞迴），子目錄天然隔離，影子絕不會被網頁／Discord／Supabase 撿到（SC-007）。同時 `verification_stats` 本來就接受 `reports_dir` 參數，指到 `shadow/` 即可重用。
- **成本閘門**：跑影子前檢查 `month_remaining() - 預估影子成本(NT$15) >= brief_degrade_reserve_twd`，不夠就跳過並記錄 `skipped: budget`——確保影子永遠不會把之後的主晨報推進節儉模式（FR-017a、SC-006）。影子成本照實 `record_cost(provider="openai")`。
- **日上限副作用**：主晨報 ~26 + 影子 ~12 ≈ 38，日上限 45 只剩 ~7 給 `/ask`。試驗期接受這點並寫進 quickstart；若問答被擋太多，再把日上限同步暫調。
- **試驗結束**：`openai_shadow_until`（日期，空＝關閉）。月上限暫調 `MONTHLY_COST_LIMIT_TWD=1000`，憲章 II 註明試驗結束日後恢復 800。不寫自動改 `.env` 的程式——到期後影子自動停（不再花錢），上限恢復由人在結案 commit 中改回並同步四處（與 2.1.0 修訂流程一致）。

## R10. 比較指令

- **Decision**：新增 `python -m reports.provider_compare [--since YYYYMMDD] [--until YYYYMMDD]`，讀 `REPORTS_DIR/morning_*.json`（主）與 `REPORTS_DIR/shadow/*_openai.json`（影子），**只取兩邊都有的同一天**做配對比較，輸出：配對天數、各自平均／中位 `brief_twd`（影子以 decision_twd + 主報告的 retrieval_twd 計，召回只付一次）、裁決率、guardrail error_count、平均 output/reasoning tokens、三條切換門檻（SC-005）是否成立。
- 裁決率直接呼叫 `verification_stats` 既有的彙總函式，不重寫分類邏輯。

## R11. /ask

- **Decision**：`ask.py` 的分支由 `llm.name == "anthropic"` 改為 `llm.name in ("anthropic", "openai")`，`model_used` 依 provider 取 `claude_model_chat` / `openai_model_chat`。OpenAI 問答 tools = 只有 hosted `web_search`（max 由 prompt 指示 + `max_tool_calls`）；**不給** `web_fetch`（問答沒有 facts pack，白名單為空，給了只會被拒）。意圖分類維持 Gemini。
- 注意影子模式只影響晨報；/ask 用 `LLM_DECISION_PROVIDER`，試驗期維持 anthropic。

## R12. 測試策略（無 key）

- 所有 OpenAI 呼叫經 `openai_client._client()` singleton；測試以 monkeypatch 換成假 client，回 `SimpleNamespace` 組成的 response（同 `test_claude_client.py` 慣例）。
- `fetch_tool` 的網路層以 `httpx.MockTransport` 測，DNS 解析以 monkeypatch `socket.getaddrinfo` 測 SSRF。
- 基線 543 passed 必須維持；預設設定下的晨報與 /ask 測試不得改動期望值（SC-002）。
