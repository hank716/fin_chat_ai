# Feature Specification: 每日市場晨報

**Feature Branch**: `006-morning-brief`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §6；憲章 I（Gemini-only）、II（成本紀律）、III（fail-closed）、IV（point-in-time）；
實作 `backend/reports/morning_brief.py`（`generate_morning_brief`）；相依 [005-ai-gemini-layer]、
[004-guardrails-symbol-guard]、[009-cost-control]；打分層來自 [012]/[013]/[014]/[015]/[016]。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。此 feature 為晨報**編排器**
> （orchestrator）：串接資料→LLM→護欄→打分→落地→推送/發布；子能力（edge/meta/sizing/qlib）另有其 spec。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 產生一份可驗證的每日晨報 (Priority: P1)

編排 features → 兩段式 grounded LLM → guardrail 清理 → 落地 JSON/Markdown/copy-for-AI，供 Web 與 Discord 呈現。

**Why this priority**: 這是產品主交付物（§6）。

**Independent Test**: 呼叫 `generate_morning_brief()` 得到 report dict，含 `report_id`、`markdown`、
`guardrail`、`cost`、`features`；`{report_id}.json`/`.md` 落地於 reports 目錄。

**Acceptance Scenarios**:

1. **Given** features 就緒，**When** `generate_morning_brief()`，**Then** 依序執行 grounded LLM →
   `run_guardrails` → 落地，回含 `report_id`/`markdown`/`guardrail`/`cost` 的 report。
2. **Given** 產報完成，**When** 讀 `latest_report_id()` / `load_report(rid)`，**Then** 取回同一份報告。
3. **Given** `report_id` 格式非法，**When** `load_report`，**Then** 回 None（路徑守衛 `REPORT_ID_RE`）。

---

### User Story 2 - 護欄與成本一起落地、可稽核 (Priority: P1)

晨報成本（研究段 grounded + 格式化段）計入全站桶並記錄；guardrail 報告與成本資訊寫進 report。

**Why this priority**: 憲章 II/III——花費透明、輸出經護欄；缺一不可信。

**Independent Test**: report 內含 `cost.brief_twd`、`cost.month_total_twd`、`guardrail.passed/error_count`。

**Acceptance Scenarios**:

1. **Given** 產報，**When** 計費，**Then** 研究段以 `grounded=True`、格式化段非 grounded 分別 `cost_of_usage` 後 `record_cost`。
2. **Given** 產報，**When** 落地，**Then** `report["guardrail"]` 與 `report["cost"]` 皆存在。

---

### User Story 3 - 打分層與回測迴圈皆 guarded、不阻斷晨報 (Priority: P2)

edge/risk/rank/qlib/meta/sizing 打分與回測迴圈以本機模型執行；任一失敗只記 warning、不影響晨報產出。

**Why this priority**: 增益而非必要；穩健性優先（晨報不可因附加分析而失敗）。

**Independent Test**: 令某打分函式拋例外，晨報仍完成並落地，只是 report 缺該 `*_scores` 欄位。

**Acceptance Scenarios**:

1. **Given** `_apply_edge_scores` 拋例外，**When** 產報，**Then** 記 warning 且晨報照常完成。
2. **Given** 無離線模型檔，**When** `_apply_qlib_scores`，**Then** 不重排（guarded no-op）。

---

### Edge Cases

- 偏空清單被模型整段略過 → `_backfill_caution` 以 movers 實際數據補齊（在 guardrail 前，符號必在資料範圍）。
- `push_discord`/`publish` 為選項：publish 時做 pCloud 冷備份 + Supabase 暖索引 + retention 清理。
- 回測迴圈 `_run_backtest_loop` 為本機運算、零 LLM 花費。
- 打分/回測全部包 try/except，錯誤不外溢。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 系統 MUST 以兩段式 grounded LLM（[005]）產生 `BriefResult`，並注入策略校準提示（若有）。
- **FR-002**: 系統 MUST 在落地前執行 `run_guardrails`（[004]）清理輸出。
- **FR-003**: 系統 MUST 計入晨報成本（研究段 grounded + 格式化段）並 `record_cost`（[009]）。
- **FR-004**: 系統 MUST 落地 `{report_id}.json` 與 `.md`，report 含 `report_id`/`report_date`/`markdown`/`copy_for_ai`/`features`/`guardrail`/`cost`。
- **FR-005**: 打分層（edge/risk/rank/qlib/meta/sizing）與回測迴圈 MUST guarded：任一失敗僅記 warning，MUST NOT 阻斷晨報。
- **FR-006**: 系統 MUST 提供讀取 API：`latest_report_id`、`load_report`、`load_markdown`、`list_reports`、`report_date_exists`。
- **FR-007**: `load_*` MUST 以 `REPORT_ID_RE` 守衛 report_id，非法格式回 None（防路徑穿越）。
- **FR-008**: `push_discord=True` 時推 Discord；`publish=True` 時 pCloud 備份 + Supabase 索引 + retention。

### Key Entities

- **report dict**: 落地的完整晨報（含 features、markdown、guardrail、cost、各 `*_scores`、`market_fear`、`backtest_summary`）。
- **BriefResult**: LLM 結構化結果（[005]）。
- **cost_info**: `brief_twd`/`tokens`/`month`/`month_total_twd`/`day_total_twd`/`monthly_limit_twd`。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 每個交易日可產出一份通過 guardrail、成本已記錄、可經 Web/Discord 呈現的晨報。
- **SC-002**: 打分/回測任一失敗時，晨報產出成功率不受影響。
- **SC-003**: 落地報告可由 `report_id` 穩定回讀；非法 id 一律拒絕。
- **SC-004**: 晨報單篇成本落在預算內（現況約 NT$8/篇）。

## Assumptions

- features 由 [003-feature-processing] 提供、point-in-time 正確。
- 單機、同步流程；一次產一份報告（`morning_{YYYYMMDD_HHMMSS}`）。
- Discord/publish 為選用旁路，失敗不影響核心報告落地（publish 內 retention 失敗僅 warning）。
