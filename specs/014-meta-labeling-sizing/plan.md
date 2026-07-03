# Implementation Plan: Meta-labeling + 部位 sizing + 市場曝險

**Branch**: `014-meta-labeling-sizing` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

在 [013] 方向 edge 之上加風險側：`_triple_barrier` 產標籤 → meta 模型標把握度（conviction）、risk 模型
標回撤高風險 → `_apply_sizing` 以 risk×meta 合成部位權重再乘市場恐慌曝險係數（[016]）→ 淨 P&L 回測驗證。
serving 全 guarded、純本地 CPU、零 LLM。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: pandas、numpy、scikit-learn、joblib；market_regime（[016]）
**Storage**: `storage/strategy/*` meta/risk 模型 + 部位/曝險輸出；讀行情 parquet
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器，off-peak CPU）
**Project Type**: web-service ML（風險側訓練 + serving sizing）
**Constraints**: guarded 兩道 gate；防未來洩漏；零 LLM/外部 API
**Scale/Scope**: training_set（triple-barrier）+ strategy_calibration（meta/risk 訓練）+ morning_brief（sizing）

## Constitution Check

*GATE: 對照憲章七原則。*

- **III. Fail-Closed（風險側精神）** — ✅ risk 偏「避雷」、serving guarded 兩道 gate；把握度低不重押。
- **IV. Point-in-Time** — ✅ triple-barrier 標籤/訓練防未來洩漏（同 [013] 三律）。
- **II. 成本紀律** — ✅ 純本地 CPU、零 LLM。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。
- I/V/VI — N/A。

**結論**：通過。無違規。

## Project Structure

### Documentation (this feature)

```text
specs/014-meta-labeling-sizing/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/reports/training_set.py         # _triple_barrier（標籤）
backend/reports/strategy_calibration.py # train_meta_model / train_risk_model / _live_meta_samples / _meta_model_path / _risk_model_path
backend/reports/morning_brief.py        # _apply_meta_scores / _apply_risk_scores / _apply_sizing（guarded serving）
backend/processor/market_regime.py      # market_fear_score（曝險係數，[016]）
tests/reports/                          # ⬜ 待新增
```

**Structure Decision**: 風險側（meta/risk/sizing）與方向 edge（[013]）共用 strategy_calibration/training_set
的 point-in-time 管線，但目標欄位（conviction/MAE）與用途（sizing 而非重排方向）不同；serving 在 [006]
以兩道 gate guarded。

## Complexity Tracking

> 無 Constitution 違規，免填。
