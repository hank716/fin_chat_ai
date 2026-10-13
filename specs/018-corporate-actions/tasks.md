# Tasks: 公司行動還原補完 + 行情完整性

**Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Phase 1 — 公司行動（US1、US2、US3）

- [x] T001 [US2] `backend/processor/adj_factors.py`：`compute_factor(wide=)` + `confirm_with_prices()`（FR-002、FR-003、FR-004）
- [x] T002 [US1] `backend/data_sources/finmind_loader.py`：`get_split_price()`、`get_capital_reduction()`；`adj_factors.rows_from_splits / rows_from_reductions`（FR-001）
- [x] T003 [US1] `backend/storage/local_store.py`：upsert 鍵加入 `source`，同日不同來源並存（FR-005）
- [x] T004 [US3] `adj_factors.find_unexplained_jumps()` + `repair()` + `--repair` CLI；剩餘清單落地（FR-006、FR-007）
- [x] T005 [US1] `adj_factors.refresh_recent()`：每日補近 14 天（FR-008）
- [x] T006 [P] `tests/test_adj_factors.py`：範圍外因子的驗證三情境、分割/減資還原連續性、同日並存、跳動偵測
- [x] T007 實跑補抓並驗證 SC-001–SC-003、SC-005（結果見 plan.md）

## Phase 2 — 行情完整性（US4）

- [x] T008 [US4] `backend/data_sources/backfill_tw_market.py`：`_try(strict=True)`、`backfill_dates()`（FR-011）
- [x] T009 [US4] `backend/data_sources/price_integrity.py`：`coverage / diagnose / purge_dates / repair` + CLI（FR-012）
- [x] T010 [US4] `backend/reports/morning_brief.py`：例行維護加入近 45 天自癒（FR-013）
- [x] T011 [P] `tests/test_price_integrity.py`：嚴格日期、缺日/幽靈日偵測、錯置日須群聚、purge 只動指定日期
- [x] T012 實跑：移除 5 個幽靈日、重抓 79 個交易日，驗證 SC-006、SC-007

## Phase 3 — 歸因對照與部署

- [x] T013 同截止日（2026-10-08）重跑評估快照，與 `post019` 對照（FR-010、SC-004）——risk 5 日 AUC +0.020、小型股 sleeve alpha +0.40、事件窗排序力歸零；對照表見 `EVAL_BASELINE.md`
- [ ] T014 rebuild backend image 部署；隔日排程晨報確認自癒與增量補抓的 log

## 後續（不在本期）

- `configs/tw_holidays.json` 未涵蓋部分休市日（元旦、颱風假），目前靠「上櫃有無資料」判斷交易日
- 上櫃端的錯置列（2026-09-20 週日有上櫃來源的列）成因未查
- 1.0–1.25 倍的小幅減資偵測不到
