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
- [X] T008 全市場回補跑完：2430/2430 檔（100%）、29,290 事件、2160 檔有股利、factor ∈ (0.5484, 1.0]（SC-001）

## Phase 2 — 讀取端還原 + 三處切換（WP1.2，已實作）

- [X] T009 `local_store.read_prices(adjusted)` + `_apply_adjustment`：OHLC backward 累積還原（FR-003）
- [X] T010 訓練端 `training_set._symbol_long`/`_index_trailing`/`current_market_regime` 改 `adjusted=True`（FR-004）
- [X] T011 `backtest.py` 雙軌：`evaluate_item(entry_adj/fwd_adj)` 報酬用還原、觸價用原始；scorecard 加 `price_basis`（FR-004）
- [X] T012 `tw_features._price_block(df, df_adj)` serving：return/dist_ma/volatility 還原價；**顯示 close 維持原始**（FR-004）
- [X] T013 [P] test：已知除息日 `return_1d_pct` 不再假跳空（SC-003）
- [X] T014 [P] test：`df_adj=None` 退回原始（train/serve 缺因子時一致，D4）（SC-004）
- [X] T015 [P] test：`_price_block` 顯示 close 原始、報酬用還原（SC-004）
- [X] T016 回歸：全套 79 測試綠（conftest fake_read_prices 加 adjusted kwarg）（SC-003）

## Phase 3 — 重訓 + 歸因對照（WP1.2）

- [X] T017 `eval_snapshot --tag adj_prices --max-date 2026-07-03`：同 max_date 重跑（`20260706_214519_adj_prices.json`）（FR-006）
- [X] T018 `--compare baseline adj_prices`：delta 表入 commit（risk +0.005~0.009、smallcap-h5 +0.0074、edge +0.007；h20 rank 噪音級退）（SC-005）
- [ ] T019 晨報實跑：rebuild backend image 部署還原碼後，`POST /brief/morning` 確認顯示名目價、guardrail 正常（FR-004）

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 因子表（WP1.1） | T001–T008 | ✅ 完成（2430/2430 回補） |
| US2 讀取端還原（WP1.2） | T009–T016 | ✅ 程式+測試 |
| US3 重訓歸因（WP1.2） | T017–T019 | 🟡 對照✅ / T019 待 rebuild 後驗 |
