# Implementation Plan: 市場恐慌 regime — TAIFEX P/C ratio + 恐慌 gauge

**Branch**: `016-market-regime-taifex` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

`taifex_loader.py` 抓 TAIFEX 選擇權 Put/Call Ratio（OpenAPI 增量 + CSV 2 年回填），過 rate_limiter 落地
`_taifex/pcr.parquet`（trade_date upsert）；`market_regime.py` 產訓練/serving 一致的市場特徵，並以透明
分位計算恐慌 gauge，供 [014] 曝險覆蓋與 [006] 顯示。市場級序列不當個股特徵。純本地、零 LLM。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: httpx、pandas；rate_limiter（[001]）；local parquet（[002]）
**Storage**: `storage/local_parquet/tw/_taifex/pcr.parquet`
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器）
**Constraints**: point-in-time upsert；市場級不當橫斷面特徵；gauge 無擬合；零 LLM
**Scale/Scope**: `taifex_loader.py`（166）+ `market_regime.py`（72）

## Constitution Check

*GATE: 對照憲章七原則。*

- **IV. Point-in-Time** — ✅ trade_date upsert；gauge 用全史百分位（asof 對齊）。
- **II. 成本紀律** — ✅ 量極小、過 rate_limiter；純本地、零 LLM。
- **V. Local-First（parquet upsert）** — ✅ 仿 fundamentals_history 自寫 parquet（[002]）。
- **III. Fail-Closed（穩健延伸）** — ✅ CSV 取不到退回 OpenAPI seed（降級不失敗）；gauge 無擬合避免小樣本誤導。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。

**設計取捨（非違規）**：市場級序列對橫斷面標籤近乎無效，刻意只用於 regime/曝險。**結論**：通過。

## Project Structure

### Documentation (this feature)

```text
specs/016-market-regime-taifex/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/data_sources/taifex_loader.py   # fetch_pc_recent / fetch_pc_history / refresh_recent / backfill / _upsert / read_pcr
backend/processor/market_regime.py       # pc_feature_frame / latest_pc_features / market_fear_score
backend/reports/morning_brief.py         # 消費 market_fear（[014] sizing / 顯示）
backend/data_sources/rate_limiter.py     # taifex bucket（[001]）
tests/data_sources/ , tests/processor/   # ⬜ 待新增
```

**Structure Decision**: 資料抓取（taifex_loader）與特徵/gauge（market_regime）分工；以 parquet 為契約。
gauge 採透明分位而非 ML，符合「市場時序樣本小、不硬擬」的判斷。

## Complexity Tracking

> 無 Constitution 違規，免填。
