# Tasks: serving 分數融合優先序 + 小型股 sleeve

**Feature**: `019-score-fusion` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成；`[ ]` = 未竟。`[P]` = 可平行。

## Phase 1 — 方向分數融合（WP2.2，已實作）

- [X] T001 `strategy_calibration.fuse_scores`：edge/rank/qlib 過 gate → z-score 加權（超 gate 幅度）→ 單一排序分數（FR-001）
- [X] T002 `_meta_margin`：模型『最佳過 gate 窗 (metric−gate)』作融合權重（FR-001）
- [X] T003 `morning_brief` 方向融合單一入口（取代 `_apply_edge/rank/qlib` 逐一 sort），watchlist 只重排一次（FR-002）
- [X] T004 report JSON 保留個別 `edge/rank/qlib_scores` + 新增 `fused_scores`/`fusion_weights`（FR-002）
- [X] T005 [P] test：單模型過 gate=該排序 / 多模型加權 / 全不過回空 / margin=0 排除（`tests/test_score_fusion.py`）

## Phase 2 — 小型股 sleeve 正式化（WP2.3，已實作）

- [X] T006 `_rank_model_path(h, band)` / `_rank_meta_path(band)`：band 模型檔名 + meta 路徑（FR-004）
- [X] T007 `_build_rank_dataset(h, path)`：訓練集路徑參數化（主池 / 小型股帶）（FR-004）
- [X] T008 `train_rank_model(band)` + `_ensure_smallcap_training_set`：小型股帶（[5M,50M)）訓練+落地 meta（FR-004）
- [X] T009 `score_rank` 依候選 `_amount` 分流（<50M→smallcap 帶、其餘→主池）；`_score_rank_band` 單帶打分（FR-003）
- [X] T010 `fuse_scores` rank 融合權重取主池/小型股帶較強者（FR-001, FR-003）
- [X] T011 每日管線 `_run_backtest_loop` 加 `train_rank_model(band="smallcap")`（FR-004）
- [X] T012 [P] test：`score_rank` 依 amount 分流；band 模型/meta 路徑（`tests/test_score_fusion.py`）
- [X] T013 live：小型股帶模型落地——h5 rank_ic **0.0673**（過 gate，icir 0.258, n=21513）vs 主池 0.0335；h20 0.0225（未過）（SC-003）

## Phase 3 — 部署 + 歸因

- [X] T014 rebuild backend image + up -d（部署融合/分流碼）
- [ ] T015 晨報實跑（可選，~NT$8）：report JSON 含 fused_scores/fusion_weights（若有方向模型過 gate）；guardrail 正常（SC-001）

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 方向融合（WP2.2） | T001–T005 | ✅ 程式+測試 |
| US2 小型股 sleeve（WP2.3） | T006–T012 | ✅ 程式+測試 / T013 live⏳ |
| 部署+歸因 | T014–T015 | ⬜ 待做 |
