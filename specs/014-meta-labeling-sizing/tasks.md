# Tasks: Meta-labeling + 部位 sizing + 市場曝險

**Feature**: `014-meta-labeling-sizing` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 標籤與模型（基線，已實作）

- [X] T001 `_triple_barrier`：上/下/時間障礙標籤 `backend/reports/training_set.py`（FR-001）
- [X] T002 `train_meta_model`/`_live_meta_samples`：conviction 把握度模型（不改方向）`backend/reports/strategy_calibration.py`（FR-002）
- [X] T003 `train_risk_model`：未來 h 日 MAE 中位數切分（FR-003）

## Phase 2 — serving sizing / 曝險（基線，已實作）

- [X] T004 `_apply_meta_scores`：標 conviction 供 sizing/過濾（[006]）（FR-002, FR-006）
- [X] T005 `_apply_risk_scores`：標高風險偏多 + 強化避雷排序（FR-003, FR-006）
- [X] T006 `_apply_sizing`：risk×meta 合成權重 × market_fear 曝險係數（[016]）、兩道 gate guarded（FR-004, FR-006）
- [X] T007 淨 P&L 回測：風險側分數 → 損益證據（FR-005）

## Phase 3 — 測試基線（未竟，高優先）

- [ ] T008 建立 OHLC/樣本 fixtures + monkeypatch 模型路徑
- [ ] T009 [P] test：`_triple_barrier` 先觸上/下/時間障礙的標籤正確（US1 / FR-001）
- [ ] T010 [P] test：meta 標把握度但方向不變（US1 / FR-002 / SC-001）
- [ ] T011 [P] test：`_apply_risk_scores` 對偏多標高風險、強化避雷（US2 / FR-003）
- [ ] T012 [P] test：`_apply_sizing` 兩道 gate 未過 → no-op；過 gate → 權重×曝險（US3 / FR-004, FR-006）
- [ ] T013 [P] test：market_fear 高 → 總曝險下降（US3 / FR-004）
- [ ] T014 test：淨 P&L 回測把風險側分數對應到損益（US / FR-005 / SC-003）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T015 conviction/risk 分數與實際 P&L 的校準監控
- [ ] T016 `/speckit-converge` 掃描 meta/risk/sizing 與本 spec 落差

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 triple-barrier + meta | T001–T002, T004, T009–T010 | 程式✅ / 測試⬜ |
| US2 風險模型 | T003, T005, T011 | 程式✅ / 測試⬜ |
| US3 sizing × 曝險 | T006–T007, T012–T014 | 程式✅ / 測試⬜ |
