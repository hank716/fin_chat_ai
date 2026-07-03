# Feature Specification: 成本控制 — 全站 AI 花費追蹤與上限

**Feature Branch**: `009-cost-control`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §25；憲章 Principle II（成本紀律）、I（Gemini-only）；
實作 `backend/cost/tracker.py`；呼叫端 `backend/api/ask.py`、`backend/reports/morning_brief.py`、
`backend/api/brief.py`（status + calibrate）、`bot`/`discord_summary`；相關 commit `052de68`、`c105f3b`、`2e4fc93`。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 全站花費不超過每日/每月上限 (Priority: P1)

所有 Gemini 呼叫（晨報 + 所有人的問答）累進到全站當日與當月桶；查詢前檢查上限，逾限婉拒。

**Why this priority**: 憲章 II 硬性預算約束（每日 NT$30 / 每月 NT$600）；避免帳單失控。

**Independent Test**: 把月桶灌到 ≥ 上限，呼叫 `check_budget()` 回 `(False, 原因, ...)`；`/ask` 收到
逾限回覆且 `budget_exceeded=True`，不再發起昂貴的 grounded 呼叫。

**Acceptance Scenarios**:

1. **Given** `month_total ≥ monthly_cost_limit_twd`，**When** `check_budget()`，**Then** 回 `False` 且原因含月額度用完。
2. **Given** `today_total ≥ daily_cost_limit_twd` 且月額度未滿，**When** `check_budget()`，**Then** 回 `False` 且原因含今日額度用完。
3. **Given** 兩者皆未滿，**When** `check_budget()`，**Then** 回 `True`。

---

### User Story 2 - 精確費用換算（cache/級距/grounding） (Priority: P1)

每筆呼叫以「未命中 input×input 價 + 命中 cache×cache 價 + output×output 價」估算，非
`totalTokens×單一價`；Pro 有 >200k 級距（加倍）；Google 搜尋 grounding 每月前 5,000 次免費、之後 $14/1k。

**Why this priority**: 估算失準會讓上限形同虛設；這是後台落差三大來源的修正（commit `052de68`）。

**Independent Test**: 對已知 usage 與模型呼叫 `cost_of_usage`，比對預期 TWD（含 cache 折扣、級距、grounding 邊際費）。

**Acceptance Scenarios**:

1. **Given** pro 模型且 `input_tokens > 200000`，**When** 估價，**Then** 套用 `large` 級距費率。
2. **Given** `cached_tokens = C`（C ≤ input），**When** 估價，**Then** 帳單 input 只算 `input−C`，C 以 cache 價。
3. **Given** `grounded=True` 且當月 grounding 次數 > 5,000，**When** 估價，**Then** 加 `$14/1000 × 匯率`。

---

### User Story 3 - 對齊後台的月度校準 (Priority: P2)

估算與 Google 後台必有落差；管理者可用後台實際金額重設當月基準，讓首頁橫幅貼近真實。

**Why this priority**: 讓對外顯示的花費可信；校準端點需權限保護。

**Independent Test**: 以有效 `X-Admin-Token` 呼叫校準端點，`month_total()` 變為傳入值且保留 TTL；
未設 `ADMIN_TOKEN` 時端點 fail-closed（503），token 不符 401。

**Acceptance Scenarios**:

1. **Given** 有效管理權杖，**When** 呼叫校準端點帶 `month_total_twd=X`，**Then** 月桶被覆寫為 X 且 TTL 保留/補回。
2. **Given** 未設定 `ADMIN_TOKEN`，**When** 呼叫校準端點，**Then** 回 503（端點停用）。

---

### Edge Cases

- **預算原子性不變式**：`/ask` 中 `check_budget() → record_cost()` 之間 MUST NOT 有 `await/yield`；
  同一 process 同步阻塞使 check-then-spend 對本地請求原子（家用單 worker）。跨 process（排程晨報）
  為軟上限、超支有界，可接受。
- **管理放行**：帶有效 `X-Admin-Token` 的 `/ask` 跳過上限，但花費**照常計入**桶。
- redis 讀寫失敗：`_get` 回 0.0、`record_cost` 靜默記 warning（不擋主流程）。
- 隔日/跨月：桶 key 含日期（`cost:day:YYYYMMDD` 48h TTL、`cost:month:YYYYMM` 70d TTL）自然汰換。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `estimate_cost_twd` MUST 以 (未命中 input×in 價 + cached×cache 價 + output×out 價 + tool_tokens×in 價) 計；級距依 prompt 總量（>200k → large）。
- **FR-002**: `cost_of_usage` MUST 為 token→金額的唯一入口；`grounded=True` 時 MUST 加 grounding 當次邊際費用（超免費額才計）。
- **FR-003**: 費率表 MUST 對齊 Google 官方定價，並標注對應 `*-latest` 別名的當前版本與核對日期；別名改指時 MUST 同步更新。
- **FR-004**: `record_cost` MUST 累加到當日與當月全站桶並設 TTL（日 48h、月 70d）。
- **FR-005**: `check_budget` MUST 同時檢查每月與每日上限；任一逾限回 `False` 與原因。
- **FR-006**: `record_grounding_request` MUST 以當月計數判斷免費額（前 5,000 次/月免費）。
- **FR-007**: `set_month_total` MUST 覆寫當月桶為傳入值並保留/補回 TTL（校準）。
- **FR-008**: 校準端點 MUST 權限保護：未設 `ADMIN_TOKEN` → fail-closed（503）；token 不符 → 401；比對 MUST 用 `secrets.compare_digest`。
- **FR-009**: `/ask` 的 check→spend 之間 MUST NOT await；管理放行 MUST 仍計入花費。
- **FR-010**: 上限值 MUST 可由 `.env` 覆寫（`DAILY_COST_LIMIT_TWD`、`MONTHLY_COST_LIMIT_TWD`）。

### Key Entities

- **cost bucket（redis）**: `cost:day:YYYYMMDD`、`cost:month:YYYYMM`、`cost:grounding:YYYYMM`。
- **usageMetadata**: Gemini 回傳用量（input/output/cached/tool tokens），由 `gemini_client._usage_of` 整理。
- **pricing table** `_PRICING`: 每模型 family（pro/flash/flash-lite）× 級距（small/large）→ (input, output, cached) USD/1M。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 全站當月花費不超過 `MONTHLY_COST_LIMIT_TWD`（軟上限下超支有界且可解釋）。
- **SC-002**: 估算涵蓋 cache 折扣、>200k 級距、grounding 三項；與後台落差可由校準收斂。
- **SC-003**: 校準端點在未設 token 時 100% 停用（無未授權寫入）。
- **SC-004**: 逾限時不再發起昂貴 grounded 呼叫（`/ask` 早退）。

## Assumptions

- 家用單 worker 規模；本地請求同步阻塞，故 check-then-spend 原子（見 Edge Cases）。
- 匯率固定估算 `_USD_TWD = 32.0`；大幅波動時人工調整。
- redis 為花費狀態儲存；跨 process 花費以桶累加聚合。
- LLM 僅 Gemini（憲章 I），故單一費率表即可涵蓋 serving 成本。
