# Tasks: 策略成效量測 — 晨報回測 + 校準回灌迴圈

**Feature**: `012-strategy-eval-backtest` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 回測引擎（基線，已實作）

- [X] T001 `evaluate_item`/`evaluate_report`：觸目標/止損、方向、報酬、MFE/MAE、超額 `backend/reports/backtest.py`（FR-002）
- [X] T002 `_forward_window`/`_index_forward_return`：未來窗嚴格 `trade_date > as_of`（FR-001）
- [X] T003 `_entry_close`：進場價=資料日收盤（缺值回退 parquet）
- [X] T004 `run_due_evaluations` + `_data_matured`/`_fully_matured`：冪等只評到期窗、落地 scorecard（FR-003）
- [X] T005 `featurize`：point-in-time 特徵向量 + 標籤存進 scorecard（FR-004）

## Phase 2 — 校準與成效評估（基線，已實作）

- [X] T006 `build_calibration_block`/`_compose_text`：成績→繁中校準文字（不足回空）`backend/reports/strategy_calibration.py`（FR-005）
- [X] T007 `evaluate_effectiveness`/`_eval_verdict`：超額統計 + 充分性 verdict（FR-006）
- [X] T008 `_run_backtest_loop` 於 [006] 每日觸發（本地、零 LLM）（FR-007）

## Phase 3 — 測試基線（未竟，高優先）

- [ ] T009 建立 scorecard/parquet fixtures（已到期/未到期窗）
- [ ] T010 [P] test：未來窗嚴格 > as_of，過去資料不洩漏（US1 / FR-001 / SC-001）
- [ ] T011 [P] test：觸目標/止損/方向/MFE/MAE/超額計算正確（US1 / FR-002）
- [ ] T012 [P] test：`run_due_evaluations` 冪等、只評到期、重跑不重複（US1 / FR-003 / SC-004）
- [ ] T013 [P] test：`build_calibration_block` 足夠→有文字、不足→空字串（US2 / FR-005）
- [ ] T014 test：`evaluate_effectiveness` verdict 隨樣本量/跨度變化（US3 / FR-006）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T015 scorecard schema 版本化（featurize 欄位變更相容）
- [ ] T016 `/speckit-converge` 掃描 backtest.py / strategy_calibration.py 與本 spec 落差

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 回測評分 | T001–T005, T010–T012 | 程式✅ / 測試⬜ |
| US2 校準回灌 | T006, T008, T013 | 程式✅ / 測試⬜ |
| US3 成效評估 | T007, T014 | 程式✅ / 測試⬜ |
