# Tasks: Qlib 離線整合（隔離 image + gate 守護 serving）

**Feature**: `015-qlib-offline` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 離線評估/打分（基線，已實作）

- [X] T001 `dump.py`：本機 parquet → Qlib 格式 `qlib_offline/dump.py`
- [X] T002 `_walk_forward`/`_rank_metrics`：purged walk-forward + 方向 rank-IC/ICIR `qlib_offline/run.py`（FR-001, FR-002）
- [X] T003 `_risk_label`/`_eval_per_horizon`：未來 h 日 MAE OOS AUC（FR-002）
- [X] T004 `evaluate` `--eval`：落地 `qlib_eval.json`（FR-001）
- [X] T005 `_train_and_score`/`score` `--score`：打分流動性股池 + `qlib_scores/{date}.json` + `qlib_meta.json`（FR-003）
- [X] T006 `--loop`：立即 eval+score + 每日 off-peak 03:00 自帶排程（FR-006）

## Phase 2 — serving 隔離與 gate（基線，已實作）

- [X] T007 `_apply_qlib_scores`：讀 JSON、過 rank-IC gate 才重排、無檔/未過不動 `backend/reports/morning_brief.py`（FR-004）
- [X] T008 serving 無 `import qlib`；隔離 Dockerfile 相依不進 serving image（FR-005）

## Phase 3 — 測試基線（未竟，中優先）

- [ ] T009 [P] test（serving）：`_apply_qlib_scores` 無檔 → no-op（US2 / FR-004 / SC-002）
- [ ] T010 [P] test（serving）：`qlib_meta` 未過 rank-IC gate → 不重排（US2 / FR-004）
- [ ] T011 [P] test（serving）：過 gate → 依分數重排偏多（US2）
- [ ] T012 [P] 靜態檢查：backend 匯入圖無 qlib（US3 / FR-005 / SC-003）
- [ ] T013 test（離線）：`_walk_forward` 無未來洩漏；rank 指標定義對齊 [013]（US1 / FR-002 / SC-001）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T014 qlib_meta gate 閾值與實際 OOS 表現的監控
- [ ] T015 `/speckit-converge` 掃描 qlib_offline/ 與 serving gate 與本 spec 落差

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 離線評估 | T001–T004, T013 | 程式✅ / 測試⬜ |
| US2 訓練/打分/gate | T005, T007, T009–T011 | 程式✅ / 測試⬜ |
| US3 隔離 + 自帶排程 | T006, T008, T012 | 程式✅ / 測試⬜ |
