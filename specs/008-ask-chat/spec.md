# Feature Specification: 問答 — Ask（意圖分類 + 討論串記憶）

**Feature Branch**: `008-ask-chat`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §20；憲章 I（Gemini-only）、II（成本紀律）、III（fail-closed 管理端點）；
實作 `backend/api/ask.py`（`POST /ask`）、`backend/chat/history.py`；相依 [005-ai-gemini-layer]、
[009-cost-control]；相關 commit `5736b48`、`3fea5b9`。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 有預算才回答、逾限婉拒 (Priority: P1)

問答前 `check_budget`；逾限回婉拒且 `budget_exceeded=True`；花費（含意圖分類）計入全站桶。

**Why this priority**: 憲章 II——避免帳單失控；成本原子性不變式（check→spend 不得 await）。

**Independent Test**: 月/日桶灌滿後呼叫 `/ask`，回逾限訊息、`cost_twd=0`、`budget_exceeded=True`。

**Acceptance Scenarios**:

1. **Given** 已達上限且無管理權杖，**When** `/ask`，**Then** 回婉拒、`budget_exceeded=True`、不發昂貴 grounded 呼叫。
2. **Given** 有效 `X-Admin-Token`，**When** 已達上限，**Then** 放行測試但花費仍計入桶。

---

### User Story 2 - 只回答財經問題（意圖過濾） (Priority: P1)

以 Flash-Lite 意圖分類過濾閒聊；非財務題直接婉拒（省大 prompt 與 grounding）。分類器 fail-open。

**Why this priority**: 直接省 token（憲章 II）；分類器不得成單點故障。

**Independent Test**: 問「今天天氣如何」→ 回「只回答財經問題」婉拒，僅計入極小分類成本；分類器出錯時當財務題續走。

**Acceptance Scenarios**:

1. **Given** `enable_intent_filter` 開，**When** 問非財務題，**Then** 早退婉拒、`cost_twd`＝分類成本。
2. **Given** 分類器 HTTP/解析失敗，**When** `/ask`，**Then** fail-open 視為財務題續走。

---

### User Story 3 - 以晨報為事實基礎 + on-demand 補充 + 討論串記憶 (Priority: P2)

以最新晨報為 grounding 靜態脈絡（明確快取省 token）；對問題中台股代號 on-demand 抓基本面；
Discord 討論串（有 conversation_id）帶最近數輪對話記憶，一般頻道無狀態。

**Why this priority**: 提升答題相關性與追問體驗；同時控成本、控故障面。

**Independent Test**: 帶 conversation_id 連問兩題，第二題可用代名詞追問；無晨報時回 404。

**Acceptance Scenarios**:

1. **Given** 尚無晨報，**When** `/ask`，**Then** 回 404「請先產生晨報」。
2. **Given** 問題含台股代號，**When** `/ask`，**Then** on-demand 抓該股基本面併入 prompt；FinMind 退避時略過該區塊、照常回答。
3. **Given** 有 conversation_id，**When** 答完，**Then** 寫入討論串記憶（截斷、限 MAX_TURNS 輪、TTL 3 天）。
4. **Given** 明確快取與本次請求（tools）不相容（400），**When** `/ask`，**Then** 降級為完整 prompt（仍吃隱式快取）。

---

### Edge Cases

- **成本原子性不變式**：`check_budget() → record_cost()` 之間 MUST NOT await（見 [009]）。
- FinMind 退避（額度/封鎖）→ 缺基本面但問答不失敗。
- Gemini 429 → 503「配額用盡」；其他 GeminiError → 503「暫時無法使用」。
- 討論串記憶讀寫失敗 → 回空/靜默（記憶是加分項，不阻斷問答）。
- 答案尾端 MUST 附輔助參考免責句；寫入記憶存「乾淨答案」（不含免責句）。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `/ask` MUST 先 `check_budget`；逾限且無管理權杖 MUST 婉拒並回 `budget_exceeded=True`。
- **FR-002**: 管理放行（有效 `X-Admin-Token`）MUST 跳過上限但花費仍計入；比對 MUST 用 `secrets.compare_digest`，未設 token 一律不放行。
- **FR-003**: 意圖分類（若啟用）MUST 過濾非財務題並早退；MUST fail-open（分類器錯誤當財務題）。
- **FR-004**: MUST 以最新晨報為 grounding；無晨報回 404。
- **FR-005**: MUST 對問題中台股代號 on-demand 抓基本面；FinMind 退避時 MUST 略過該區塊而非失敗。
- **FR-006**: MUST 用明確快取（[005]）省 token；不相容（400）MUST 降級為完整 prompt。
- **FR-007**: 有 conversation_id MUST 帶討論串記憶並於答後寫入（截斷、限 `MAX_TURNS`、TTL 3 天）；無則無狀態。
- **FR-008**: MUST 計入問答成本（含分類）並 `record_cost`（[009]）；check→spend 之間 MUST NOT await。
- **FR-009**: 回覆 MUST 附輔助參考免責句；寫入記憶 MUST 存不含免責句的乾淨答案。
- **FR-010**: Gemini 429 MUST 回 503（配額用盡）；其他 GeminiError MUST 回 503。

### Key Entities

- **AskRequest / AskResponse**: 問答請求/回應（question、user_id、conversation_id / answer、cost_twd、today_spent、daily_limit、budget_exceeded）。
- **討論串記憶（redis list）**: `chat:hist:{conversation_id}`，最近 `MAX_TURNS` 輪 `{q,a}`，TTL 3 天。
- **static_block / variable_block**: prompt 穩定前綴（可快取）+ 每題變動尾段。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 逾限時 0 次昂貴 grounded 呼叫；花費不超過上限（軟上限、超支有界）。
- **SC-002**: 非財務題被過濾，僅付極小分類成本；分類器故障不影響答題成功率。
- **SC-003**: 明確快取/FinMind/記憶任一故障皆降級續行，問答成功率不受影響。
- **SC-004**: 討論串內可用代名詞追問；一般頻道維持無狀態（不洩漏跨會話內容）。

## Assumptions

- 家用單 worker，同步阻塞，故 check→spend 原子（[009]）。
- 事實以晨報/即時查詢為準；記憶只提供追問脈絡。
- 台股代號以 `_TW_CODE` 正則擷取，最多取前 3 檔抓基本面。
