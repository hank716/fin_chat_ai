# Implementation Plan: 策略成效量測 — 晨報回測 + 校準回灌迴圈

**Branch**: `012-strategy-eval-backtest` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

`backtest.py` 對過去晨報候選以已落地 parquet 做 point-in-time 回測（未來窗嚴格 > as_of），聚合成
scorecard 落地並存 featurize 向量；`strategy_calibration.py` 把成績濃縮成校準文字（[006] 注入 prompt）
並做成效顯著性評估。全程本地 CPU、零 LLM。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: pandas、numpy；local_store（讀 parquet）
**Storage**: `storage/backtests/{report_id}.json`（scorecard）；讀 `storage/reports/*.json`
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器，off-peak CPU）
**Project Type**: web-service 分析/迴圈
**Performance Goals**: CPU 數秒；冪等可重跑
**Constraints**: 零未來洩漏；零 LLM/外部 API
**Scale/Scope**: `backtest.py`（440）+ `strategy_calibration.py` 文字校準/評估部分（~1413 中的量測環）

## Constitution Check

*GATE: 對照憲章七原則。*

- **IV. Point-in-Time** — ✅ 未來窗嚴格 `trade_date > as_of`；進場價用資料日收盤。
- **II. 成本紀律** — ✅ 純本地 CPU、零 LLM/外部 API（以時間換運算）。
- **III. Fail-Closed（延伸）** — ✅ 成效評估帶充分性 verdict，避免小樣本誤導決策。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。
- I/V/VI — N/A。

**結論**：通過。無違規。

## Project Structure

### Documentation (this feature)

```text
specs/012-strategy-eval-backtest/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/reports/backtest.py            # evaluate_report / run_due_evaluations / featurize / _aggregate
backend/reports/strategy_calibration.py# build_calibration_block / evaluate_effectiveness / _compose_text
backend/reports/morning_brief.py       # _run_backtest_loop + 注入校準（[006]）
backend/storage/local_store.py         # read_prices（回測行情）
tests/reports/                         # ⬜ 待新增（與 006 共用目錄）
```

**Structure Decision**: 「量測（backtest）」與「校準/評估（strategy_calibration 文字環）」分工；ML 模型環
留在 [013]/[014]。scorecard 作為 backtest → 訓練之間的穩定契約（含 featurize 向量）。

## Complexity Tracking

> 無 Constitution 違規，免填。
