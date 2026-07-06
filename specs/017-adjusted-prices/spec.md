# Feature Specification: 除權息還原 — 事件因子表 + 讀取端還原

**Feature Branch**: `017-adjusted-prices`

**Created**: 2026-07-06

**Status**: In progress（US1/WP1.1 已實作；US2–US3/WP1.2 待做）

**來源交叉引用**: `OPTIMIZATION_PLAN.md` WP1.1+WP1.2、全域決策 D2（事件因子表 + 讀取端還原）、
D4（FEATURE_COLUMNS 紀律）；憲章 IV（point-in-time）、V（parquet upsert，不重寫既有檔）、II（零 LLM）。
消費者：[013] training_set、[012] backtest、[003/tw_features] serving。baseline 對照見
`storage/strategy/eval_history/20260706_162137_baseline.json`（max_date=2026-07-03）。

> **問題**：TWSE/FinMind 都用原始 close，除息跳空污染所有 return/波動/MA 特徵、triple-barrier 標籤與
> 回測 P&L——單一最大準確度污染源。**策略（D2）**：建「除權息事件因子表」，**讀取端**據此還原；
> **不重寫既有 parquet、不並存 adj_close 欄**。使用邊界：報酬/波動/MA/標籤/movers 排行用還原價；
> **觸價判定（target_hit/stop_hit）與所有對使用者顯示的價格用原始價**。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 除權息事件因子表（WP1.1） (Priority: P1) ✅

逐檔（FinMind 免費 tier 僅 per-stock）取 `TaiwanStockDividendResult` 的除權息參考價，算
`adj_factor = after_price / before_price`，過 sanity（∈ (0.5, 1.0]）後 upsert 落地
`storage/local_parquet/tw_adj_factors.parquet`（key=(symbol, ex_date)）。一次性回補走 rate_limiter、
斷點續跑（checkpoint；遇 quota/ban 停手下輪續）；每日增量以分桶輪替刷新（掛 morning_brief guarded 區）。

**Why this priority**: 是 US2 讀取端還原的資料基礎；先有正確因子表才能談還原。

**Independent Test**: `build()` 回補、`read_adj_factors(sym)` 讀回、`refresh_recent()` 增量；
`compute_factor` 公式與 sanity 單元測試。

**Acceptance Scenarios**:

1. **Given** tw/ 既有個股，**When** `build()`，**Then** 因子表列數 > 0，每列 factor ∈ (0.5, 1.0]。
2. **Given** 2330（每季配息）/0056（高配息）/含配股個股，**When** 讀回因子，**Then** 手算誤差 <0.1%。
3. **Given** 回補中遇 FinMind quota/ban，**When** 再次 `build()`，**Then** 從 checkpoint 續跑不重打已完成檔。

---

### User Story 2 - 讀取端還原 + 訓練/回測/serving 三處切換（WP1.2） (Priority: P1) ⏳

`read_prices(adjusted=True)` 依因子表對 open/high/low/close 做 backward 累積還原（ex_date 前所有價
×∏factor）。訓練端（`_symbol_long`/`_index_trailing`）改 `adjusted=True`；backtest 雙軌（報酬/MFE/MAE/
vs_index 用還原價，target_hit/stop_hit 維持原始價）；serving（tw_features return/dist_ma/volatility/
movers）用還原價，**顯示欄位 close/目標價/止損價維持原始價**。訓練與 serving **同一 PR 切換**（D4）。

**Why this priority**: 因子表若不接讀取端則無效果；這步才真正消除除息跳空污染。

**Independent Test**: 已知除息日 `return_1d_pct` 不再假跳空；洩漏測試全綠；train/serve 欄位 parity。

**Acceptance Scenarios**:

1. **Given** 有除息事件的個股，**When** `read_prices(adjusted=True)`，**Then** ex_date 前價被累積還原、
   跨除息日報酬連續（無假跳空）。
2. **Given** serving，**When** 產晨報，**Then** 顯示的 close/目標/止損為原始名目價（非還原價）。

---

### User Story 3 - 重訓 + 歸因對照（WP1.2） (Priority: P2) ⏳

切換後在 **同一 max_date=2026-07-03** 重跑 `eval_snapshot --tag adj_prices`，與 baseline `--compare`。
**不設「必須變好」門檻**——這是正確性修復；落地逐指標 delta 報告即可。

**Why this priority**: D1 歸因方法論要求改動前後同資料快照對照，證明修復影響（而非盲改）。

**Independent Test**: `eval_snapshot --compare baseline adj_prices` 逐指標 delta 表落地。

**Acceptance Scenarios**:

1. **Given** baseline 快照，**When** 切換還原價重訓，**Then** 同 max_date delta 表落地入 commit/PR。

---

### Edge Cases

- FinMind 免費 tier 市場級查詢需付費 level（回 400）→ 回補/增量只能 per-stock、過 rate_limiter。
- 因子 sanity ∈ (0.5, 1.0]：配息只下修參考價（factor<1）；極端配股才逼近 0.5；越界丟棄並 log。
- 因子表放 `local_parquet/` 根（非 tw/ 內），避免被 training_set `tw/*.parquet` glob 誤當個股。
- 每日增量分桶輪替（~14 天覆蓋全 universe）；新除權息在 lookback 窗內補上，讀取層容忍此延遲。
- 顯示價一律原始名目價；還原僅供內部報酬/波動/標籤運算。
- 純本地運算、零 LLM。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: MUST 由 `TaiwanStockDividendResult` 逐檔取除權息參考價，算 `after/before` 因子，sanity
  ∈ (0.5, 1.0]，upsert 落地 `tw_adj_factors.parquet`（key=(symbol, ex_date)）。
- **FR-002**: 回補 MUST 過 rate_limiter、支援斷點續跑（checkpoint；遇 quota/ban 停手）；增量 MUST 掛
  morning_brief guarded 區、分桶輪替（不阻斷晨報）。
- **FR-003**: `read_prices(adjusted=True)` MUST 對 OHLC 做 backward 累積還原；`adjusted=False`（預設）
  維持原始價。
- **FR-004**: 訓練/回測/serving 的報酬/波動/MA/標籤/movers MUST 用還原價；**target_hit/stop_hit 觸價與
  所有顯示價 MUST 用原始價**。
- **FR-005**: 訓練端與 serving 端 MUST 同一 PR 切換並附兩端 parity 測試（D4）。
- **FR-006**: 切換後 MUST 在同一 max_date 產 `adj_prices` 快照並與 baseline 對照（D1）；全流程零 LLM。

### Key Entities

- **tw_adj_factors.parquet**: `storage/local_parquet/tw_adj_factors.parquet`；schema `symbol / ex_date /
  adj_factor / source`。
- **adj_factor**: `after_price / before_price` ∈ (0.5, 1.0]。
- **還原價**: read-time backward 累積（ex_date 前 ×∏factor），不落地、不並存欄位。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 因子表覆蓋 tw/ 有股利個股，列數 > 0，全部 factor ∈ (0.5, 1.0]（US1 ✅）。
- **SC-002**: 抽查 2330/0056/配股個股 factor 手算誤差 <0.1%（US1 ✅）。
- **SC-003**: 已知除息日的 `return_1d_pct` 不再假跳空；洩漏測試全綠（US2）。
- **SC-004**: 顯示價維持原始名目價；train/serve 欄位 parity（US2）。
- **SC-005**: 同 max_date 的 baseline↔adj_prices delta 表落地（US3）。

## Assumptions

- 資料抓取/落地機制對齊 [001] rate_limiter、[002] storage；baseline 快照見 `EVAL_BASELINE.md`。
- 除權息事件特徵（days_to_ex_dividend / dividend_yield）為副產品，屬 [020-feature-batch]（本 spec 不含）。
