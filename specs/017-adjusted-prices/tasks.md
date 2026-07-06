# Tasks: 除權息還原 — 事件因子表 + 讀取端還原

**Feature**: `017-adjusted-prices` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成；`[ ]` = 未竟。`[P]` = 可平行。

## Phase 1 — 除權息事件因子表（WP1.1，已實作 + live 驗證）

- [X] T001 `finmind_loader.get_dividend_result`：`TaiwanStockDividendResult` 逐檔取 before/after 參考價（FR-001）
- [X] T002 `adj_factors.compute_factor`/`rows_from_results`：`after/before` + sanity ∈ (0.5, 1.0]（FR-001）
- [X] T003 `local_store.ADJ_FACTORS_PATH`/`read_adj_factors`/`write_adj_factors`：(symbol, ex_date) upsert（FR-001）
- [X] T004 `adj_factors.build`：全市場回補、過 rate_limiter、checkpoint 斷點續跑、遇 quota/ban 停手（FR-002）
- [X] T005 `adj_factors.refresh_recent`：分桶輪替增量，掛 `morning_brief._run_backtest_loop` guarded 區（FR-002）
- [X] T006 [P] test `tests/test_adj_factors.py`：公式/sanity 邊界/結果過濾/upsert 冪等/斷點續跑（14 例綠）
- [X] T007 live 驗收：2330（每季配息 45 筆）/0056（高配息 ~0.977）/含配股（低至 0.7683）factor 手算誤差 <0.1%（SC-001, SC-002）
- [ ] T008 全市場回補跑完（背景，resumable）：`build()` 覆蓋 tw/ 全 universe，記錄最終列數/覆蓋率（SC-001）

## Phase 2 — 讀取端還原 + 三處切換（WP1.2，待做）

- [ ] T009 `local_store.read_prices(adjusted: bool = False)`：讀因子表對 OHLC backward 累積還原（FR-003）
- [ ] T010 訓練端 `training_set._symbol_long`/`_index_trailing` 改 `adjusted=True`（triple-barrier/fwd 標籤繼承）（FR-004）
- [ ] T011 `backtest.py` 雙軌：`forward_return/mfe/mae/vs_index` 還原價、`target_hit/stop_hit` 原始價；scorecard 加 `price_basis`（FR-004）
- [ ] T012 `tw_features.py` serving：return/dist_ma/volatility/movers 還原價；**顯示 close/目標/止損維持原始價**（FR-004）
- [ ] T013 [P] test：已知除息日 `return_1d_pct` 不再假跳空（US2 / SC-003）
- [ ] T014 [P] test：train/serve 欄位 parity（還原價路徑一致，D4）（US2 / SC-004）
- [ ] T015 [P] test：顯示欄位（close/target/stop）為原始價、非還原價（US2 / SC-004）
- [ ] T016 回歸：既有洩漏測試套件全綠（不因還原引入未來資訊）（SC-003）

## Phase 3 — 重訓 + 歸因對照（WP1.2，待做）

- [ ] T017 `eval_snapshot --tag adj_prices --max-date 2026-07-03`：同 max_date 重跑（訓練端 D4 同步後）（FR-006）
- [ ] T018 `eval_snapshot --compare <baseline> <adj_prices>`：逐指標 delta 表落地入 commit/PR（US3 / SC-005）
- [ ] T019 晨報實跑：`POST /brief/morning` 確認 tw_watchlist 顯示名目價、guardrail summary 無異常（FR-004）

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 因子表（WP1.1） | T001–T008 | 程式✅ / 測試✅ / 回補⏳(背景) |
| US2 讀取端還原（WP1.2） | T009–T016 | ⬜ 待做 |
| US3 重訓歸因（WP1.2） | T017–T019 | ⬜ 待做 |
