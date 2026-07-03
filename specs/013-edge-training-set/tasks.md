# Tasks: Edge 模型 — 歷史回放訓練集 + per-horizon 方向 edge

**Feature**: `013-edge-training-set` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 歷史回放訓練集（基線，已實作）

- [X] T001 `build_training_set`/`_build_big`/`_emit_all`：逐交易日回放 movers 選股 `backend/reports/training_set.py`（FR-001）
- [X] T002 `_symbol_long`/`_emit_samples`：point-in-time 特徵 + 5/20 日標籤（`close.shift(-h)`）（FR-002）
- [X] T003 防洩漏三律：特徵/排行 `≤ D`、標籤 `> D`、（訓練端 walk-forward）（FR-002）
- [X] T004 `_overlap_weights`：重疊窗降權（FR-006）
- [X] T005 `build_if_stale`：訓練集過期才重建（FR-007）

## Phase 2 — per-horizon 方向模型（基線，已實作）

- [X] T006 `train_edge_model`/`_train_target`：HistGradientBoosting per-horizon 落地 `backend/reports/strategy_calibration.py`（FR-003）
- [X] T007 `_evaluate_oos`/`_precision_at_k`/`_fit_calibrator`：walk-forward OOS + 機率校準（FR-004）
- [X] T008 樣本不足自動跳過、退回文字校準（[012]）（FR-003）
- [X] T009 `_apply_edge_scores`/`_apply_rank_scores`：guarded serving 打分/重排（[006]）（FR-005）

## Phase 3 — 測試基線（未竟，高優先）

- [ ] T010 建立行情 parquet fixtures（多日多股）
- [ ] T011 [P] test：訓練集特徵 `≤ D`、標籤 `> D`（無洩漏）（US1 / FR-002 / SC-001）
- [ ] T012 [P] test：回放選股與線上 movers 同規則（門檻/排除 ETF/top）（US1 / FR-001）
- [ ] T013 [P] test：`_overlap_weights` 對重疊窗降權（FR-006）
- [ ] T014 [P] test：樣本不足 → `train_edge_model` 跳過（US2 / FR-003 / SC-002）
- [ ] T015 [P] test：`_apply_edge_scores` 無模型 → no-op；例外 → warning 續行（US3 / FR-005 / SC-003）
- [ ] T016 test：walk-forward 切分無時間洩漏；precision@k 計算正確（US2 / FR-004）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T017 訓練集/模型的黃金樣本回歸（特徵欄位變更相容）
- [ ] T018 `/speckit-converge` 掃描 training_set.py / strategy_calibration.py 與本 spec 落差

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 回放訓練集 | T001–T005, T011–T013 | 程式✅ / 測試⬜ |
| US2 per-horizon edge | T006–T008, T014, T016 | 程式✅ / 測試⬜ |
| US3 guarded serving | T009, T015 | 程式✅ / 測試⬜ |
