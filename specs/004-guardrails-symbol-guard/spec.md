# Feature Specification: 輸出護欄 — Symbol Guard 與六道驗證

**Feature Branch**: `004-guardrails-symbol-guard`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §18、§30；憲章 Principle III（Guardrail Fail-Closed, NON-NEGOTIABLE）；
實作 `backend/guardrails/verify.py`；呼叫端 `backend/reports/morning_brief.py:348`；相關 commit `7e7fd1a`。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 捏造內容一律移除 (Priority: P1)

晨報讀者（投資人）看到的每個數據、候選標的、新聞，都必須能對應到真實 features 資料；
LLM 幻覺出來的欄位/標的/新聞不得出現在最終報告。

**Why this priority**: 錯誤的投資訊號比缺少訊號傷害更大（憲章 III）。這是護欄存在的核心理由。

**Independent Test**: 給一份含捏造 `source_ref`、不存在標的、查無來源新聞的 `BriefResult`，
執行 `run_guardrails` 後，這些片段全部被移除，且 report `violations` 有對應 error 紀錄。

**Acceptance Scenarios**:

1. **Given** evidence 的 `source_ref` 指向 features 不存在的路徑，**When** 執行護欄，**Then**
   該 evidence 從 section 移除、`counts.evidence_dropped` +1、新增一筆 `metric` error。
2. **Given** 候選標的 symbol 不在 `features.tw.stocks`，**When** 執行護欄，**Then** 該候選移除、
   `counts.symbols_dropped` +1、新增一筆 `symbol` error。
3. **Given** news_digest 某則無法以 url 或 title 比對到 `features.news`，**When** 執行護欄，**Then**
   該新聞移除並記為疑似捏造。

---

### User Story 2 - Symbol Guard 上游失敗時 fail-closed (Priority: P1)

當台股資料上游失敗、`features.tw.stocks` 為空時，代表「沒有任何可驗證的合法符號」，此時模型給的
候選一律無法比對，**必須全部移除**，而非放行。

**Why this priority**: 放行等於護欄失效（憲章 III）。這是 commit `7e7fd1a` 的核心修正。

**Independent Test**: 傳入 `features.tw.stocks == {}` 與含候選的 result，執行後所有 `tw_watchlist`/
`tw_caution` 皆被移除，理由標示「資料範圍為空（台股資料疑似缺失）」。

**Acceptance Scenarios**:

1. **Given** `features.tw.stocks` 為空且 result 有 3 個候選，**When** 執行護欄，**Then** 3 個全移除、
   每筆 error detail 含「資料範圍為空」。

---

### User Story 3 - 禁語標記與資料時效 (Priority: P2)

過度承諾/保證語（「保證獲利」「穩賺」）與必然因果語（「必漲」「隔日一定」）於敘事中出現時，
標記為 warning（不刪內容）；報告缺 `data_as_of` 時記為 error。

**Why this priority**: 合規與可信度；依使用者決策，本工具可給方向/目標價/止損，故不再攔「建議買賣」
指令，只擋誇大保證與必然因果。

**Independent Test**: 敘事含「穩賺」→ 產生 `advice` warning、`counts.phrase_warnings` +1，但文字保留。

**Acceptance Scenarios**:

1. **Given** headline 含「一定會漲」，**When** 執行護欄，**Then** 新增 `causality` warning，內容不變。
2. **Given** result 無 `data_as_of`，**When** 執行護欄，**Then** 新增 `data_age` error。

---

### Edge Cases

- `source_ref` 後黏中文註解或 `=值`（模型常見）→ 於首個 `（ ( = 或空白` 處截斷後再解析（`_ANNOT_RE`）。
- JSONPath 方言：根前綴 `features`／`$` 可省略；支援字串鍵 bracket（`["2408"]`、`["航運業"]`、
  `["SOX~NASDAQ"]`）與數字索引（`[0]`）。僅放寬語法，欄位仍須真實存在。
- 新聞比對成功但模型未帶 url → 以來源回填 url、tier、provider，不因缺 url 而丟。
- 新聞缺 source/date/title 任一 → 移除。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 系統 MUST 對每個 `evidence.source_ref` 解析至 `features`；解析為 `_MISSING` 者 MUST 移除該 evidence 並記 `metric` error。
- **FR-002**: 系統 MUST 校驗候選標的（`tw_watchlist`/`tw_caution`）symbol ∈ `features.tw.stocks`；不符者 MUST 移除。
- **FR-003**: 當 `features.tw.stocks` 為空，系統 MUST fail-closed——移除所有候選，MUST NOT 放行。
- **FR-004**: 系統 MUST 校驗 news_digest 可經 url 或 title 比對到 `features.news`，且具備 source/date/title；否則移除。
- **FR-005**: 系統 MUST 掃描所有敘事文字的 `ADVICE_BANNED` 與 `CAUSALITY_BANNED`，命中記為 warning（不刪內容）。
- **FR-006**: 系統 MUST 在缺 `data_as_of` 時記 `data_age` error。
- **FR-007**: 系統 MUST 回傳清理後的 `BriefResult` 與 report（`passed`、`counts`、`error_count`、`warning_count`、`violations`）；`passed = (error_count == 0)`。
- **FR-008**: 系統 MUST NOT 變更輸入物件（以 `model_copy(deep=True)` 操作副本）。
- **FR-009**: 解析 source_ref MUST 容忍 JSONPath 方言（省略根前綴、字串鍵/數字索引 bracket、尾註解截斷），但欄位存在性 MUST 嚴格校驗。

### Key Entities

- **BriefResult**: LLM 產出的結構化晨報（sections/evidence、tw_watchlist、tw_caution、news_digest、
  headline、risks、data_as_of）。定義於 `backend/ai/schemas.py`。
- **features**: 由 processor 產生的 point-in-time 結構化資料（`tw.stocks`、`news`、各市場指標）。
- **guardrail report**: 稽核結果（counts / violations / passed），存入 report JSON 並於頁面顯示攔截狀態。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 任何捏造的 evidence/symbol/news 100% 不出現在清理後的 `BriefResult`。
- **SC-002**: 當台股資料缺失（stocks 空）時，候選輸出數為 0（fail-closed），無漏放行。
- **SC-003**: 護欄為純函式、無副作用；相同輸入產生相同 report（可重現、可測）。
- **SC-004**: 每次攔截皆有可稽核紀錄（`violations` 內含 guard 類別、severity、detail）。

## Assumptions

- features 為權威且 point-in-time 正確的資料來源（見 `003-feature-processing`、憲章 IV）。
- 護欄在 LLM 產出之後、報告落地之前呼叫（現況於 `morning_brief.py` 產報流程）。
- 本工具定位為研究輔助，可給方向/目標價/止損，故不攔買賣指令；僅擋保證/必然語（使用者決策）。
- schema 允許 news url 為 None（比對成功後回填）。
