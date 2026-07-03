# Implementation Plan: 特徵運算 — 技術/籌碼/基本面/跨市場（point-in-time）

**Branch**: `003-feature-processing` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

讀落地 parquet（[002]），以 pandas 算純數字特徵：台股 index/stocks/sectors/movers（技術面 + 籌碼面 +
相對強弱）、跨市場報酬相關性、on-demand 基本面衍生指標。全程 point-in-time（trade_date 對齊、無未來日）、
不呼叫 LLM，輸出 JSON-safe dict 供 [005]/[004]/[006] 消費。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: pandas、pyarrow；`functools.lru_cache`；FinMind loader（[001]）
**Storage**: 讀 `storage/local_parquet/*`（[002]）；on-demand 基本面走 FinMind（[001]）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器）
**Project Type**: web-service 運算層
**Performance Goals**: 焦點標的 on-demand + lru，控 FinMind 呼叫次數
**Constraints**: 只算數字不判讀；point-in-time；NaN→None；單一資料源失敗不阻斷
**Scale/Scope**: `backend/processor/*.py`（tw_features 383 / fundamentals 361 / prefetch 218 / …）

## Constitution Check

*GATE: 對照憲章七原則。*

- **I. Gemini-only（分工）** — ✅ 特徵層只算數字、不呼叫 LLM；判讀交給 [005]。
- **IV. Point-in-Time** — ✅ trade_date 對齊、未來日由 [002] 寫入閘擋、基本面日曆感知略過。
- **II. 成本紀律** — ✅ on-demand + lru + 焦點標的，控 FinMind 額度（配合 [001]）。
- **III. Fail-Closed（延伸）** — ✅ 衍生指標以 features 路徑暴露，供 [004] guardrail 驗證（捏造欄位被攔）。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。

**結論**：通過。無違規。

## Project Structure

### Documentation (this feature)

```text
specs/003-feature-processing/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/processor/
├── tw_features.py          # build_tw_features / _price_block / _chip_block / _movers / build_adhoc_symbol
├── fundamentals.py         # build_fundamentals（月營收 + 季財報衍生指標，lru，日曆感知）
├── fundamentals_history.py # 基本面歷史（餵訓練集）
├── prefetch_fundamentals.py# 焦點/全市場預抓到磁碟快取
├── intermarket_features.py # 跨市場報酬相關性（US/BTC）
└── market_regime.py        # TAIFEX P/C ratio / 恐慌 gauge（[016]）
backend/storage/local_store.py  # 讀取 parquet + 未來日寫入閘（[002]）
tests/processor/            # ⬜ 待新增
```

**Structure Decision**: 依維度拆檔（技術/籌碼在 tw_features、基本面在 fundamentals、跨市場在
intermarket）；純函式、輸入 parquet/FinMind、輸出 dict，便於離線單元驗證衍生指標數值。

## Complexity Tracking

> 無 Constitution 違規，免填。
