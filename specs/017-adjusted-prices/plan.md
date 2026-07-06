# Implementation Plan: 除權息還原 — 事件因子表 + 讀取端還原

**Branch**: `017-adjusted-prices` | **Date**: 2026-07-06 | **Spec**: [spec.md](./spec.md)

**Note**: 正向新規格（`OPTIMIZATION_PLAN.md` WP1.1+WP1.2）。US1/WP1.1 已實作並 live 驗證；
US2–US3/WP1.2 待做。所有架構決策已在 `OPTIMIZATION_PLAN.md` 全域決策 D2/D4 定案。

## Summary

建除權息事件因子表（`after/before` 參考價比）作 SSOT，讀取端 backward 累積還原以消除除息跳空對
報酬/波動/MA/標籤/回測的污染。因子表**不重寫既有 parquet、不並存 adj_close 欄**（D2）；顯示價一律
原始名目價。純本地、零 LLM；歸因走 D1（同 max_date=2026-07-03 對照 baseline）。

## Technical Context

**Language/Version**: Python 3.12（backend 容器）
**Primary Dependencies**: httpx、pandas、pyarrow；rate_limiter（[001]）；local parquet（[002]）；
FinMind `TaiwanStockDividendResult`（免費 tier 僅 per-stock）
**Storage**: `storage/local_parquet/tw_adj_factors.parquet`（+ `_adj_factors_done.json` 回補 checkpoint）
**Testing**: pytest（`tests/test_adj_factors.py`，14 例綠）
**Constraints**: sanity factor ∈ (0.5, 1.0]；顯示價原始、內部運算還原；train/serve 同 PR 切換（D4）；零 LLM
**Scale/Scope**: ~2430 檔一次性回補（rate-limited、resumable）；US1 已上，US2 觸及 training_set/backtest/tw_features

## Constitution Check

*GATE: 對照憲章七原則。*

- **IV. Point-in-Time** — ✅ 因子按 ex_date 對齊；還原為 read-time backward 累積，不引入未來資訊。
- **II. 成本紀律** — ✅ 過 rate_limiter、斷點續跑；純本地、零 LLM。
- **V. Local-First（parquet upsert）** — ✅ (symbol, ex_date) upsert；**不重寫既有價格 parquet**（D2）。
- **III. Fail-Closed** — ✅ sanity 越界丟棄；遇 quota/ban 停手不半途污染；增量 guarded 不阻斷晨報。
- **VI. 服務隔離** — ✅ 抓取只在 backend/離線容器；FastAPI 路由不直打 FinMind。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本規格先行；commit 引用 `specs/017`。

**設計取捨（非違規）**：免費 tier 無市場級查詢 → 增量採分桶輪替（~14 天覆蓋），讀取層容忍新事件的
短暫延遲（純內部運算，不影響顯示與觸價）。**結論**：通過。

## Project Structure

### Documentation (this feature)

```text
specs/017-adjusted-prices/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/data_sources/finmind_loader.py   # get_dividend_result（TaiwanStockDividendResult）        [US1 ✅]
backend/processor/adj_factors.py          # compute_factor / rows_from_results / build / refresh_recent [US1 ✅]
backend/storage/local_store.py            # ADJ_FACTORS_PATH / read_adj_factors / write_adj_factors   [US1 ✅]
backend/reports/morning_brief.py          # _run_backtest_loop guarded 掛 adj_factors.refresh_recent   [US1 ✅]
tests/test_adj_factors.py                 # 公式/sanity/upsert/斷點續跑（14 例）                       [US1 ✅]
backend/reports/training_set.py           # _symbol_long/_index_trailing adjusted=True                [US2 ⏳]
backend/reports/backtest.py               # 雙軌：報酬還原價 / 觸價原始價                             [US2 ⏳]
backend/processor/tw_features.py          # serving 報酬/波動/movers 還原價；顯示價原始               [US2 ⏳]
backend/reports/eval_snapshot.py          # --tag adj_prices 重跑 + --compare baseline                [US3 ⏳]
```

**Structure Decision**: 抓取（finmind_loader）→ 領域邏輯（processor/adj_factors）→ 儲存 IO
（storage/local_store）三層分工；讀取端還原以 `read_prices(adjusted=)` 單一開關貫穿訓練/回測/serving，
保證 train/serve 同源（D4）。

## Complexity Tracking

> 無 Constitution 違規，免填。
