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
- [x] T014 rebuild backend image 部署；隔日排程晨報確認自癒與增量補抓的 log——10/13 18:15 部署；10/15 08:32 `_price_integrity.json` 有自癒紀錄（殘留的 9/20 見 Phase 4）

## 後續（不在本期）

- `configs/tw_holidays.json` 未涵蓋部分休市日（元旦、颱風假），目前靠「上櫃有無資料」判斷交易日
- ~~上櫃端的錯置列（2026-09-20 週日）成因未查~~ → 已查明為 TWII yfinance 週日列，見 Phase 4
- 1.0–1.25 倍的小幅減資偵測不到

## Phase 4: Convergence

> 2026-10-15 盤點：自癒每天回報 `still_missing: ["2026-09-20"]`（週日）。根因是 `TWII.parquet` 有一筆 yfinance 給的週日列（volume=0、source=yfinance），`coverage()` 把它當上櫃日曆 → 9/20 被當交易日 → 判上市缺日、每天白打一次 TWSE。上方「後續」第二項所說的「上櫃端錯置列」即此列，並非上櫃來源。

- [x] T015 `backend/data_sources/price_integrity.py` `coverage()`：新增 `index` 欄（source=='yfinance'），`other` 只計非 twse、非 yfinance 的來源，讓上櫃 FinMind 才是交易日曆 per SC-006 / plan: 以上櫃來源當交易日曆 (partial)
- [x] T016 `price_integrity.diagnose()`：幽靈日判定擴大為 `other==0 & (listed>0 | index>0)`，讓既有 `repair()`→`purge_dates()` 自動清掉只有指數列的非交易日 per FR-012 (partial)
- [x] T017 `backend/data_sources/yfinance_loader.py`：market 為 `tw` 的標的丟掉週末日期（美股、加密不動） per FR-011 (partial)
- [x] T018 [P] `tests/test_price_integrity.py`：只有 yfinance 列的日期不算交易日、不算缺日、判為幽靈日並被 purge；指數列不影響上櫃日曆 per FR-012 (missing)
- [x] T019 [P] `tests/test_yfinance_loader.py`：mock `yf.Ticker().history` 含週日列 → TWII 丟掉、BTC 保留 per FR-011 (missing)
- [ ] T020 rebuild backend 部署；次一排程晨報確認 `_price_integrity.json` 的 `purged.rows_removed>=1`、`still_missing==[]`，`TWII.parquet` 無 2026-09-20 per SC-006 (missing)
