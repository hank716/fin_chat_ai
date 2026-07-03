# Tasks: Web Report Page（SSR）

**Feature**: `007-web-report` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — SSR 渲染與路由（基線，已實作）

- [X] T001 `render_report_html` + `report.html`（敘事/候選/evidence/guardrail/成本）`backend/reports/web_renderer.py`（FR-001）
- [X] T002 `render_history_html` + `history.html`（歷史列表 + cost/activity/calibration/evaluation/history 面板）（FR-001, FR-004）
- [X] T003 autoescape + `tag_label`（CLAIM_TAG 中文標籤）（FR-003）
- [X] T004 路由：`GET /`、`/report/{id}`、`/report/{id}.json`、`/report/{id}.md` `backend/api/brief.py`（FR-002）
- [X] T005 report_id 不存在 → 404（FR-005）
- [X] T006 深色模式 / RWD / 回頂（base.html）（FR-007）

## Phase 2 — 測試基線（未竟，中優先）

- [ ] T007 建立 `tests/web/`（FastAPI TestClient + 樣本 report dict）
- [ ] T008 [P] test：`GET /report/{id}` 回 200 HTML、含候選/evidence（US1 / FR-001）
- [ ] T009 [P] test：`.json`/`.md` 後綴回對應格式（US1 / FR-002）
- [ ] T010 [P] test：不存在 report_id → 404（US1 / FR-005）
- [ ] T011 [P] test：HTML autoescape 生效（含特殊字元的欄位被跳脫）（FR-003 / SC-003）
- [ ] T012 test：首頁面板缺資料時略過、不報錯（US2 / FR-004）

## Phase 3 — 後續強化（backlog，選用）

- [ ] T013 頁面快照/視覺回歸（深色/RWD）
- [ ] T014 `/speckit-converge` 掃描 web_renderer/templates 與本 spec 落差

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 單篇報告 | T001, T003–T005, T008–T011 | 程式✅ / 測試⬜ |
| US2 首頁歷史/面板 | T002, T012 | 程式✅ / 測試⬜ |
| US3 深色/RWD | T006, T013 | 程式✅ / 測試⬜ |
