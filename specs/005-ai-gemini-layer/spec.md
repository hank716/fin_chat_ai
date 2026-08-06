# Feature Specification: AI 層 — Gemini 結構化生成、grounding、快取

**Feature Branch**: `005-ai-gemini-layer`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §16、§17、§20；`ARCHITECTURE.md` §4.1；憲章 I（Gemini-only）、
II（成本紀律）、III（不輸出 raw CoT）；實作 `backend/ai/`（`gemini_client.py`、`llm_client.py`、
`schemas.py`、`prompts.py`、`gemini_cache.py`）；相關 commit `0e2ce49`、`2e4fc93`。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。此 feature 為 LLM 呼叫
> 原語層，被 [006-morning-brief]、[008-ask-chat] 消費；計費由 [009-cost-control] 承接。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 結構化 JSON 輸出（schema 強制） (Priority: P1)

所有分析輸出以 `responseMimeType=application/json` + `responseSchema` 強制為結構化 JSON，
parse 成 pydantic 模型（`BriefResult` / `AnalysisResult`），供 guardrail 與頁面消費。

**Why this priority**: 結構化輸出是 guardrail（004）與頁面渲染的前提；非 JSON 即無法驗證/呈現。

**Independent Test**: 對 `_generate_json` 餵入 mock 200 回應（合法 JSON），得到 dict + usage；
餵非 JSON 文字則 raise `GeminiError`。

**Acceptance Scenarios**:

1. **Given** Gemini 回合法 JSON，**When** `analyze_full_brief(features)`，**Then** 回 `(BriefResult, usage)`。
2. **Given** 回應非合法 JSON，**When** `_generate_json`，**Then** raise `GeminiError`（含 head 片段）。
3. **Given** 回應無 candidates 或空內容，**When** `_generate_json`，**Then** raise `GeminiError`。

---

### User Story 2 - 兩段式即時連網晨報 (Priority: P1)

突破「responseSchema 不能與 tool 並用」限制：① PRO + Google 搜尋寫分析稿（主推理連網）→
② Flash 純格式化成 `BriefResult`。回研究/格式化兩段 usage 供分別計費。

**Why this priority**: 兼顧「即時連網品質」與「結構化可驗證」；是晨報主推理路徑。

**Independent Test**: `analyze_full_brief_grounded(features)` 回 `(BriefResult, research_usage, struct_usage)`，
研究段用 `gemini_model_brief`+search、格式化段用 `gemini_model_qa`。

**Acceptance Scenarios**:

1. **Given** features，**When** grounded 晨報，**Then** 先 `generate_text(use_search=True)` 再 `_generate_json` 格式化。
2. **Given** 傳入 `calibration` 提示，**When** 研究段，**Then** 注入 prompt 讓模型依過去準確度自我修正。

---

### User Story 3 - 意圖分類 + 明確快取省 token (Priority: P2)

問答前用最便宜模型（flash-lite）判斷是否財務相關（**fail-open**：出錯一律當財務題）；當日 QA
靜態 context 存進 Gemini 明確快取（`cachedContents`，綁 report_id+model），命中部分以 cache 價計。

**Why this priority**: 直接降成本（憲章 II）；但兩者都不得成為問答單點故障（皆優雅降級）。

**Independent Test**: `classify_finance_intent` 於 HTTP/解析失敗時回 `(True, {})`；
`get_or_create_qa_cache` 於門檻不足/API 拒絕/redis 故障時回 `None`（呼叫端走完整 prompt）。

**Acceptance Scenarios**:

1. **Given** 分類器呼叫失敗，**When** `classify_finance_intent`，**Then** 回 `(True, {})`（fail-open）。
2. **Given** context 未達快取門檻（HTTP 400），**When** `get_or_create_qa_cache`，**Then** 回 `None` 並降級。
3. **Given** 引用明確快取的 generateContent，**When** 帶 `cachedContent`，**Then** MUST NOT 再送 tools（否則 400）。

---

### Edge Cases

- **HTTP 狀態分流**：503→`GeminiUnavailable`（tenacity 重試，指數退避）；429→`GeminiQuotaExceeded`
  （fail-fast 不重試）；400→`GeminiBadRequest`（不重試，呼叫端降級）。
- **usage 口徑**：`input_tokens`=promptTokenCount（已含 cached）；`output_tokens` 含 thoughtsTokenCount
  （thinking，否則嚴重低估）；`cached_tokens`、`tool_tokens` 分開回傳（對齊 009 計費）。
- 明確快取 key 版本化（`_KEY_VERSION`）：快取結構改變時 bump，避免沿用舊結構名稱。
- redis 快取名稱 TTL 比 cachedContents ttl 早 2 分鐘過期，避免引用剛過期的快取。
- `GEMINI_API_KEY` 未設定 → `_generate_json`/`generate_text` raise `GeminiError`。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 系統 MUST 以 `responseMimeType=application/json`+`responseSchema` 產生結構化輸出並 parse 成 pydantic 模型。
- **FR-002**: ~~系統 MUST 提供兩段式 grounded 晨報（PRO+search 研究 → Flash 格式化），回兩段 usage。~~
  **（superseded by [022-llm-tiering]）** 兩段式的存在僅為繞過「`responseSchema` 不能與 `tools` 並用」；
  決策層改由具 structured outputs 的供應商單次產出後，第②段格式化已移除。Gemini 路徑保留此行為
  作為降級用途。
- **FR-003**: 系統 MUST 依 HTTP 狀態分流例外：503 可重試、429/400 fail-fast，並由對應例外型別表達。
- **FR-004**: ~~系統 MUST 為唯一 LLM 供應商（Gemini）；`llm_client` 保留薄 Protocol 但 MUST NOT 接 Claude API。~~
  **（superseded by [022-llm-tiering]；憲章 2.0.0 Principle I 已重定義為分層供應商）**
  改為：Gemini MUST 為**廣度召回層**的唯一供應商（`google_search` grounding），MUST NOT 承擔決策
  （分析、選股、報告生成）；決策層由 `llm_client.get_decision_llm()` 依設定分派，Gemini 實作保留為降級路徑。
- **FR-005**: 意圖分類器 MUST fail-open（任何錯誤回 True），MUST NOT 成為問答單點故障。
- **FR-006**: 明確快取建立失敗（門檻不足/API 拒絕/redis 故障）MUST 優雅降級回 None，問答不中斷。
- **FR-007**: 引用明確快取的請求 MUST NOT 重送 tools（tools 已放進 cachedContent）。
- **FR-008**: `_usage_of` MUST 輸出 input/output(含 thinking)/cached/tool 四欄，對齊 [009-cost-control] 計費口徑。
- **FR-009**: 系統 MUST NOT 輸出 raw chain-of-thought（憲章 III、design_docs §16.3）；只輸出結構化結論與 evidence。
- **FR-010**: 模型選擇 MUST 可由設定調整（brief=PRO、qa=Flash、classifier=Flash-Lite；`.env` 可覆寫）。

### Key Entities

- **BriefResult / BriefSection / Evidence / WatchItem / NewsDigestItem**（`schemas.py`）：晨報結構化結果。
- **GEMINI_BRIEF_SCHEMA / GEMINI_INTENT_SCHEMA / GEMINI_RESPONSE_SCHEMA**：對應 responseSchema。
- **usage dict**：`{input_tokens, output_tokens, cached_tokens, tool_tokens}`。
- **cachedContent 名稱**：`gemini:cache:{ver}:{model}:{report_id}`（redis）→ Gemini cachedContents 資源。
- **SEARCH_TOOLS**：`google_search` / `url_context` / `code_execution`。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 分析輸出 100% 為通過 schema 的結構化 JSON（可被 004 guardrail 與頁面消費）。
- **SC-002**: 分類器/明確快取任一故障時，問答成功率不受影響（皆降級續行）。
- **SC-003**: usage 口徑與 009 計費一致（含 thinking tokens、cache 分離），估算不低估。
- **SC-004**: 503 暫時過載可自動恢復（重試）；429/400 立即回清楚錯誤，不空等。

## Assumptions

- Gemini v1beta generateContent + `X-goog-api-key`（對齊使用者驗證過的 curl）。
- `*-latest` 別名指向的版本由 009 費率表對應；別名改指時兩處需同步。
- 家用規模；httpx 同步呼叫、tenacity 重試即可，無需 async 佇列。
- prompts 由 `prompts.py` 組裝（static/variable 分塊以配合明確快取）。
