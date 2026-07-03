# Tasks: 每日市場晨報

**Feature**: `006-morning-brief` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 編排核心（基線，已實作）

- [X] T001 `_build_combined_features(refresh_tw)`：組合台/美/加密 features + landed `backend/reports/morning_brief.py`
- [X] T002 注入策略校準 `strategy_calibration.build_calibration_block()`（FR-001）
- [X] T003 兩段式 grounded LLM `analyze_full_brief_grounded`（[005]）（FR-001）
- [X] T004 成本：研究段 grounded + 格式化段 → `cost_of_usage` + `record_cost`（[009]）（FR-003）
- [X] T005 `_backfill_caution`：偏空清單以 movers 補齊（guardrail 前）（edge case）
- [X] T006 `run_guardrails` 清理輸出（[004]）（FR-002）
- [X] T007 組 report dict + 落地 JSON/MD + `copy_for_ai`（FR-004）
- [X] T008 讀取 API：`latest_report_id`/`load_report`/`load_markdown`/`list_reports`/`report_date_exists`（FR-006）
- [X] T009 `REPORT_ID_RE` / `_safe_path` 路徑守衛（FR-007）

## Phase 2 — 打分層 / 發布（基線，已實作；子能力另有 spec）

- [X] T010 guarded 打分：`_apply_{edge,risk,rank,qlib,meta}_scores` + `_apply_sizing`（try/except 隔離）（FR-005）
- [X] T011 `_run_backtest_loop`（本機、零 LLM）（FR-005）
- [X] T012 選用旁路：`push_discord` 推送、`publish`（pCloud + Supabase + retention）（FR-008）

## Phase 3 — 測試基線（未竟，高優先）

- [ ] T013 建立 `tests/reports/`（features/BriefResult/tracker/guardrail 以 fixtures/monkeypatch）
- [ ] T014 [P] test：`generate_morning_brief` 依序 LLM→guardrail→落地，report 含必要欄位（US1 / FR-004）
- [ ] T015 [P] test：`load_report` 非法 report_id → None（US1 / FR-007 / 路徑穿越）
- [ ] T016 [P] test：打分函式拋例外 → 晨報仍完成、缺該 `*_scores`（US3 / FR-005 / SC-002）
- [ ] T017 [P] test：成本研究段 grounded、格式化段非 grounded，皆 record_cost（US2 / FR-003）
- [ ] T018 test：report 含 `guardrail` 與 `cost` 區塊（US2）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T019 `_backfill_caution` 補齊邏輯的單元覆蓋（movers 數據正確映射）
- [ ] T020 `/speckit-converge` 掃描 morning_brief.py 與本 spec 落差、補未列任務

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 產出可驗證晨報 | T001–T009, T014–T015 | 程式✅ / 測試⬜ |
| US2 護欄+成本落地 | T004, T006–T007, T017–T018 | 程式✅ / 測試⬜ |
| US3 打分/回測 guarded | T010–T011, T016 | 程式✅ / 測試⬜ |
