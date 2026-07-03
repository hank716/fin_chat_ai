# Feature Specification: 市場恐慌 regime — TAIFEX 選擇權 P/C ratio + 恐慌 gauge

**Feature Branch**: `016-market-regime-taifex`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: 憲章 IV（point-in-time）、II（零 LLM）、V（parquet upsert）；實作
`backend/data_sources/taifex_loader.py`、`backend/processor/market_regime.py`；被 [014]（曝險）、
[006]（市場恐慌顯示）消費；相關 commit `8772d76`。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。TAIFEX 選擇權 Put/Call Ratio
> 為**市場級單一序列**（同日對所有股同值），主要供市場 regime / 總曝險（[014]），對橫斷面標籤近乎無效。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - P/C ratio 資料管線（增量 + 歷史回填） (Priority: P1)

日常增量走 TAIFEX OpenAPI（JSON，滾動 ~21 天）；歷史回填走 pcRatioDown（POST 區間回 CSV，可拉 2 年，
取不到則退回 OpenAPI 21 天 seed）。落地 `_taifex/pcr.parquet`，依 trade_date upsert。對外請求過 rate_limiter。

**Why this priority**: 恐慌 gauge 的資料基礎；量小但需穩定落地與回填（commit `8772d76`）。

**Independent Test**: `refresh_recent()` 增量 upsert；`backfill(years=2)` 回填；`read_pcr()` 讀回序列。

**Acceptance Scenarios**:

1. **Given** OpenAPI 可用，**When** `refresh_recent`，**Then** 抓近 ~21 天並依 trade_date upsert 落地。
2. **Given** 歷史回填，**When** `backfill`，**Then** 走 CSV 拉 2 年；CSV 失敗退回 OpenAPI seed。
3. **Given** 對外請求，**When** 抓取，**Then** 過 `taifex` rate_limiter bucket（[001]）。

---

### User Story 2 - 市場特徵 frame（訓練/serving 一致定義） (Priority: P1)

`pc_feature_frame()` 產日期→市場特徵（pc_oi_ratio / pc_oi_z20 / pc_vol_ratio / pc_oi_chg5），訓練端
map-by-date；`latest_pc_features()` 產最新一日供 serving 注入（定義一致）。

**Why this priority**: 訓練與 serving 用同一定義才不會分布漂移。

**Independent Test**: `pc_feature_frame` 與 `latest_pc_features` 欄位一致（`PC_FEATURES`）。

**Acceptance Scenarios**:

1. **Given** pcr 序列，**When** `pc_feature_frame`，**Then** 產四個市場特徵欄位（by date）。
2. **Given** 最新日，**When** `latest_pc_features`，**Then** 產與訓練同定義的最新特徵。

---

### User Story 3 - 透明恐慌 gauge（分位、可解讀、無擬合） (Priority: P2)

`market_fear_score(asof)`：P/C-OI z-score 在全史的百分位（高＝避險/恐慌濃、未來市場風險偏高）。無擬合、
可解讀（市場時序樣本小，刻意不硬擬 ML）。供 [014] 曝險覆蓋與 [006] 顯示。

**Why this priority**: 把市場情緒轉成可解讀的總曝險係數；避免小樣本硬擬 ML。

**Independent Test**: `market_fear_score()` 回 0–1 分位；相同輸入可重現。

**Acceptance Scenarios**:

1. **Given** pcr 全史，**When** `market_fear_score(asof)`，**Then** 回該日 P/C-OI z-score 的歷史百分位。

---

### Edge Cases

- 市場級序列對橫斷面 risk/meta 近乎無效——只用於 regime / 總曝險（不當個股特徵）。
- CSV 歷史取不到 → 退回 OpenAPI 21 天 seed（降級不失敗）。
- upsert 仿 fundamentals_history 自寫 parquet（trade_date 對齊、point-in-time）。
- gauge 無擬合、透明分位（可解讀）；刻意不在小樣本硬擬 ML。
- 純本地運算、零 LLM。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: MUST 提供增量（OpenAPI）與歷史回填（CSV 2 年，失敗退回 OpenAPI seed）兩來源，落地 `_taifex/pcr.parquet`（trade_date upsert）。
- **FR-002**: 對外請求 MUST 過 `taifex` rate_limiter bucket（[001]）。
- **FR-003**: `pc_feature_frame` 與 `latest_pc_features` MUST 用一致定義（`PC_FEATURES`），供訓練 map-by-date 與 serving 注入。
- **FR-004**: `market_fear_score` MUST 為 P/C-OI z-score 的全史百分位（透明、無擬合、可重現）。
- **FR-005**: 市場級序列 MUST NOT 當作橫斷面個股特徵（只供 regime / 曝險）。
- **FR-006**: 落地 MUST point-in-time（trade_date 對齊）；全流程 MUST 零 LLM。

### Key Entities

- **pcr.parquet**: `storage/local_parquet/tw/_taifex/pcr.parquet`（P/C put/call OI/vol）。
- **PC_FEATURES**: `pc_oi_ratio`/`pc_oi_z20`/`pc_vol_ratio`/`pc_oi_chg5`。
- **market_fear_score**: 0–1 恐慌分位（供 [014] 曝險、[006] 顯示）。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: P/C ratio 可增量更新與回填 2 年，落地無重複列（upsert）。
- **SC-002**: 訓練/serving 市場特徵定義一致，無分布漂移。
- **SC-003**: 恐慌 gauge 透明可解讀、可重現（無擬合）。
- **SC-004**: 純本地、零 LLM；point-in-time 對齊。

## Assumptions

- 落地路徑/機制對齊 [002-storage-manager]；rate limiter 來自 [001]。
- 市場恐慌係數由 [014] 用於總曝險覆蓋、[006] 用於顯示。
- 樣本小，gauge 刻意採透明分位而非 ML。
