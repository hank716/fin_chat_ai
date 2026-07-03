# Feature Specification: Web Report Page（SSR）

**Feature Branch**: `007-web-report`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §7（Web Report Page）；憲章 III（不外洩 raw CoT）；
實作 `backend/reports/web_renderer.py`、`backend/templates/{base,report,history}.html`、
路由 `backend/api/brief.py`；相關 commit `4dd5d75`（視覺翻新/深色/RWD）、`0923678`（花費橫幅/回頂）。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 檢視單篇晨報（SSR HTML） (Priority: P1)

以 Jinja2 SSR 呈現一份晨報：敘事、候選、evidence（附 fact/計算/推論/限制標籤）、guardrail 攔截狀態、成本。

**Why this priority**: Web 是家庭成員查閱報告的主要介面（§7）。

**Independent Test**: `GET /report/{report_id}` 回 HTML；`render_report_html(report)` 以 `report.html` 渲染。

**Acceptance Scenarios**:

1. **Given** 有效 report_id，**When** `GET /report/{report_id}`，**Then** 回 SSR HTML（含 evidence 標籤 `tag_label`）。
2. **Given** 提供 `.json`/`.md` 後綴路由，**When** 請求，**Then** 分別回 JSON / 純文字 markdown。
3. **Given** report_id 不存在，**When** 請求，**Then** 回 404。

---

### User Story 2 - 首頁歷史列表 + 花費/活動/評估面板 (Priority: P2)

首頁列出歷史報告，並顯示成本橫幅、待機活動、策略校準/成效評估、歷史回補進度（面板可折疊）。

**Why this priority**: 讓使用者一眼掌握花費與系統狀態（§7、commit `0923678`/`e798846`）。

**Independent Test**: `GET /` 回 HTML；`render_history_html(reports, cost, activity, calibration, evaluation, history)`。

**Acceptance Scenarios**:

1. **Given** 有歷史報告，**When** `GET /`，**Then** 列出報告並附成本橫幅。
2. **Given** 有校準/評估資料，**When** 首頁渲染，**Then** 對應面板顯示（可折疊）。

---

### User Story 3 - 自動深色模式 + RWD (Priority: P3)

頁面支援自動深色模式與響應式排版，並提供回頂按鈕。

**Why this priority**: 體驗優化（commit `4dd5d75`/`0923678`）；非核心資料正確性。

**Independent Test**: 手機寬度下版面不破、深色模式跟隨系統。

**Acceptance Scenarios**:

1. **Given** 系統為深色，**When** 開啟頁面，**Then** 自動套深色主題。

---

### Edge Cases

- HTML autoescape 開啟（`select_autoescape(["html"])`），防注入。
- evidence claim 型別以 `CLAIM_TAG` 對應中文（fact/calculation/inference/limitation）。
- 頁面呈現 guardrail 攔截狀態；MUST NOT 顯示 raw chain-of-thought（憲章 III）。
- 報告在本機被 retention 清掉時，讀取路由觸發 pCloud 回補（見 [002]/[006]）。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: MUST 以 Jinja2 SSR 渲染單篇報告（`report.html`）與首頁歷史（`history.html`），共用 `base.html`。
- **FR-002**: MUST 提供路由 `GET /`（首頁）、`/report/{id}`（HTML）、`/report/{id}.json`、`/report/{id}.md`。
- **FR-003**: MUST 開啟 HTML autoescape；evidence 以 `tag_label` 顯示 claim 型別中文標籤。
- **FR-004**: 首頁 MUST 可顯示 cost / activity / calibration / evaluation / history 面板（資料缺則略過）。
- **FR-005**: report_id 不存在 MUST 回 404。
- **FR-006**: 頁面 MUST 顯示 guardrail 攔截狀態、MUST NOT 呈現 raw CoT。
- **FR-007**: MUST 支援自動深色模式與 RWD（增益，不影響資料正確性）。

### Key Entities

- **report dict**: 由 [006-morning-brief] 落地的完整報告。
- **templates**: `base.html`（版型）、`report.html`（單篇）、`history.html`（首頁列表 + 面板）。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 任一有效 report_id 皆可經 Web 呈現；三種格式（HTML/JSON/MD）一致。
- **SC-002**: 首頁正確列出歷史並顯示花費橫幅。
- **SC-003**: 頁面不外洩 raw CoT；HTML 內容經 autoescape。
- **SC-004**: 手機/桌機皆可讀（RWD），深色模式跟隨系統。

## Assumptions

- 報告資料由 [006] 產生；讀取/回補由 [002]/[006] 提供。
- 純 SSR（無前端框架）；模板即介面契約。
