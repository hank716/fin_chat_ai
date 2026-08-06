# Feature Specification: LLM 分層 — Claude 決策 + 連網查證，Gemini 退居廣度召回

**Feature Branch**: `022-llm-tiering`

**Created**: 2026-08-06

**Status**: In progress

**來源交叉引用**: 憲章 **2.0.0** Principle I（分層 LLM 供應商 + 交叉查證，本案觸發的修憲）、
Principle II（成本紀律）、III（fail-closed guardrail 不變）。supersedes [005] FR-002/FR-004。
依賴 [004] guardrail、[006] 晨報、[008] 問答、[009] 成本控制。
`ARCHITECTURE.md` §4.1 已同步改寫。

> **問題**：晨報的挑股與推理由 Gemini 決定，而 Gemini 在推理層偏弱；同時 Gemini 的
> `google_search` grounding 會產生幻覺（錯置日期、把分析評論當新聞、引用內容農場）。
> 由**同一個模型**同時負責召回與決策，等於沒有任何一方能稽核另一方——幻覺會被洗成
> 「有來源」的假事實直接寫進晨報。

> **解法**：分層 + 交叉查證。Gemini 只做廣度召回產「待查證線索」；Claude 做決策，
> 並用 `web_fetch` 逐條開啟召回層引用的 URL 核對後才採信。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 決策層改由 Claude 單次產出 (Priority: P1)

晨報主推理改為 `claude-opus-5` 單次呼叫：吃 features JSON + facts pack + 回測校準，
以 structured outputs 直接產出 `BriefDraft`。原本的兩段式（PRO 寫分析稿 → Flash 格式化）
第②段整個移除——它只是為了繞過 Gemini「`responseSchema` 不能與 `tools` 並用」的限制。

**Why this priority**: 這是本案的產品價值來源（推理與選股品質），其餘 WP 都在支撐它。

**Independent Test**: 給定固定 features + facts，呼叫回傳通過 `BriefDraft` 驗證的物件，
且 `tw_watchlist` / `tw_caution` 各 5 檔。

**Acceptance Scenarios**:

1. **Given** 正常 features + facts，**When** `draft_brief`，**Then** 回合法 `BriefDraft`，
   且 LLM **未**產出 `risk_score` / `conviction_score` / `size_weight`（那三個由本地 ML 事後填）。
2. **Given** Claude 拋 `LLMError` 或 `stop_reason=="refusal"`，**When** 晨報產製，
   **Then** 自動退回 Gemini 兩段式路徑，JSON 記 `decision_provider="gemini-fallback"`，晨報仍完成。
3. **Given** 送出的 payload，**When** 檢查，**Then** **不含** `temperature`/`top_p`/`top_k`
   （Opus 5 已移除這些參數，帶了直接 400）。

---

### User Story 2 - 交叉查證閉環 (Priority: P1)

Gemini 產出的每則 event 在 prompt 中標記為**待查證線索**。決策層必須用 `web_fetch`
開啟其 `url` 確認原文支持該敘述，結果寫入 `fact_checks`：
`confirmed` / `contradicted` / `unverifiable`。後兩者不得進入 narrative 或 news_digest。

**Why this priority**: 這是「Claude 能連網」的唯一目的；沒有它，分層只是換個模型寫作文。

**Independent Test**: 晨報 JSON 的 `fact_checks` 非空；被標為
`contradicted`/`unverifiable` 的 URL 不出現在 narrative/news_digest。

**Acceptance Scenarios**:

1. **Given** facts pack 含一則 URL 打不開的 event，**When** 產製晨報，
   **Then** 該則標 `unverifiable` 且**不**出現在敘事，只能在 risks 提及資料限制。
2. **Given** facts pack 的某則 event 無 `url`，**When** retrieval 組裝，**Then** 該則直接丟棄
   （無法查證的東西不進決策層）。
3. **Given** 連續數日 `fact_checks` 100% `confirmed`，**When** 人工抽驗，
   **Then** 需確認模型確實有 fetch 而非敷衍（SC-004 的監控意義）。

---

### User Story 3 - 廣度召回層產出結構化事實包 (Priority: P2)

`ai/retrieval.py::fetch_facts()` 用 Gemini flash + `google_search` 產出 `FactsPack`
（`events: [{claim, date, source, url, verified}]`），隨晨報 JSON 落地供重放與 A/B。
召回層 prompt 只允許「列出事實 + 來源」，不得做分析或選股。

**Why this priority**: 決策層的輸入契約；同時把既有死碼 `fetch_web_context` 與
`FULL_BRIEF_RULES` 中懸空的 `features.web_context` 契約一併修好。

**Acceptance Scenarios**:

1. **Given** Gemini 回應含 `groundingMetadata`，**When** 解析，**Then** 取得**完整** URL 清單
   （不是 `_grounding_sources` 給 Discord 用的前 4 筆截斷版）。
2. **Given** `enable_facts_pack=False`，**When** 產製晨報，**Then** 退回純 Gemini 舊路徑。

---

### User Story 4 - /ask 切 Claude，快取預設關閉 (Priority: P3)

問答改走決策層，掛較低的 `max_uses`（4/2，互動情境對延遲敏感）。
意圖分類器**維持** Gemini flash-lite（trivial gate、fail-open、token 極少）。

**Why this priority**: chat 實際用量稀疏，價值與風險都低於晨報。

**Acceptance Scenarios**:

1. **Given** `enable_claude_prompt_cache=False`（預設），**When** 送出請求，
   **Then** payload **不得**含 `cache_control`——稀疏使用下寫入 premium 永遠回不了本。
2. **Given** 設為 True，**When** 5 分鐘內連問 3 題，**Then** 第 2/3 題
   `cache_read_input_tokens > 0`。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 決策層 MUST 由 `llm_client.get_decision_llm()` 依 `LLM_DECISION_PROVIDER` 分派；
  `anthropic`（預設）與 `gemini`（降級/A-B 對照）兩個實作都 MUST 存在。
- **FR-002**: 決策層 MUST 以 structured outputs 單次產出，MUST NOT 沿用兩段式格式化。
- **FR-003**: 決策層 MUST NOT 送出 `temperature` / `top_p` / `top_k`，MUST NOT 使用 assistant prefill。
- **FR-004**: 決策層 MUST 使用 streaming（`max_tokens` 遠大於 16k 時非 streaming 會撞 SDK timeout）。
- **FR-005**: 決策層 MUST 處理 `stop_reason=="pause_turn"`（server tool 迭代上限）並以
  `max_continuations` 收斂；MUST NOT 讓 paused turn 靜默截斷成一份殘缺晨報。
- **FR-006**: 決策層 MUST 在讀取 `content` **之前**檢查 `stop_reason=="refusal"`。
- **FR-007**: 連網工具 MUST 設 `max_uses`（晨報 12/5、問答 4/2），MUST NOT 另外宣告
  `code_execution`（`_20260209` 版內建 dynamic filtering，重複宣告會造成兩個執行環境）。
- **FR-008**: `web_fetch` MUST NOT 啟用 `citations`（與 `output_config.format` 互斥，會 400）。
- **FR-009**: 召回層 MUST 只輸出帶 source URL 的事實線索；無 URL 的 event MUST 丟棄。
- **FR-010**: 未通過查證（`contradicted` / `unverifiable`）的 event MUST NOT 進入 narrative
  或 news_digest。
- **FR-011**: 成本計價 MUST 依供應商分流——Anthropic 的 `input_tokens` **不含** cache，
  與 Gemini 的 `promptTokenCount`（**已含** cache）語意相反，MUST NOT 共用同一套減法。
- **FR-012**: 成本計價 MUST 涵蓋 cache write premium 與 server tool 按次計費。
- **FR-013**: 決策層失敗時 MUST 降級回 Gemini 路徑，晨報 MUST NOT 因供應商故障而整份失敗。
- **FR-014**: `enable_claude_prompt_cache` 預設 MUST 為 `False`（見 SC-005 的成本理由）。
- **FR-015**: 意圖分類器 MUST 維持 Gemini flash-lite，MUST NOT 改用決策層模型。

### Key Entities

- **FactsPack / FactEvent**（`ai/retrieval.py`）：召回層輸出，`{claim, date, source, url, verified}`。
- **BriefDraft / WatchItemDraft / FactCheck**（`ai/schemas.py`）：決策層 structured output 目標；
  **排除** `risk_score` / `conviction_score` / `size_weight`（本地 ML 事後填）。
- **DecisionLLM**（`ai/llm_client.py`）：`draft_brief` / `answer_question` 兩方法的 Protocol。
- **LLMError / LLMUnavailable / LLMQuotaExceeded / LLMBadRequest**（`ai/errors.py`）：
  供應商中立例外；`GeminiError` 家族改為繼承之，既有 `except GeminiError` 呼叫點不受影響。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 晨報 100% 產出（決策層故障時由降級路徑補上），`decision_provider` 欄位如實記錄。
- **SC-002**: 寫入 narrative/news_digest 的外部事件 100% 可在 `fact_checks` 找到 `confirmed`。
- **SC-003**: 成本估算與 Anthropic Console 實際金額偏差 < 20%。
- **SC-004**: `contradicted + unverifiable` 比例成為每日可觀測指標；持續為 0% 需人工抽驗
  （代表模型敷衍查證，而非召回層完美）。
- **SC-005**: 預設設定下，`/ask` payload 不含 `cache_control`——避免稀疏使用時無聲支付
  1.25×/2× 的快取寫入 premium 卻從未讀取。

## Assumptions

- 預算配置為「晨報優先、問答讓路」（Jay 決策）：晨報吃掉月度額度大半是預期行為；
  額度耗盡時 `/ask` 被擋屬正常，非故障。
- `claude-opus-5` 為決策層預設；chat 若需省錢可降 `claude-sonnet-5`（用量少、影響面窄）。
- Gemini 仍是廣度召回的唯一供應商——Google 索引對台股中文冷門標的的覆蓋是真優勢，
  Claude 的 `web_search` 在此只是查證補漏工具，不是召回主力。
- 家用單機規模；沿用同步呼叫。
