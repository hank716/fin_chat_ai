# Implementation Plan: Edge 模型 — 歷史回放訓練集 + per-horizon 方向 edge

**Branch**: `013-edge-training-set` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

`training_set.py` 在歷史上回放線上選股規則，產出與線上同分布、point-in-time 且無未來洩漏的訓練集；
`strategy_calibration.py` 用回放 + 線上樣本訓練 per-horizon HistGradientBoosting 方向模型（walk-forward
OOS），落地供 [006] serving 端 guarded 打分/重排。純本地 CPU、零 LLM。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: pandas、numpy、scikit-learn（HistGradientBoosting）、joblib
**Storage**: 訓練集 parquet + `storage/strategy/*` 模型檔；讀行情 parquet
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器，off-peak CPU）
**Project Type**: web-service ML（離線訓練 + serving 打分）
**Constraints**: 三律防洩漏；guarded serving；零 LLM/外部 API
**Scale/Scope**: `training_set.py`（499）+ `strategy_calibration.py` edge/rank 訓練環

## Constitution Check

*GATE: 對照憲章七原則。*

- **IV. Point-in-Time** — ✅ 特徵/排行 `≤ D`、標籤 `> D`、walk-forward 切分（三律）。
- **II. 成本紀律** — ✅ 純本地 CPU、零 LLM（GTX 1060 硬體現實下用表格式 ML）。
- **III. Fail-Closed（穩健延伸）** — ✅ serving 打分 guarded：無模型/未過 gate 不動、例外只記 warning。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。
- I/V/VI — N/A（serving 不呼叫 LLM；儲存見 [002]）。

**結論**：通過。無違規。

## Project Structure

### Documentation (this feature)

```text
specs/013-edge-training-set/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/reports/training_set.py         # build_training_set / _symbol_long / _emit_samples / _triple_barrier / _overlap_weights / build_if_stale
backend/reports/strategy_calibration.py # train_edge_model / _train_target / _fit_predict / _evaluate_oos / _precision_at_k / _fit_calibrator
backend/reports/morning_brief.py        # _apply_edge_scores / _apply_rank_scores（guarded serving）
tests/reports/                          # ⬜ 待新增
```

**Structure Decision**: 訓練集產生（training_set）與模型訓練/評估（strategy_calibration）分工；serving 打分
在 [006] 以 guarded 呼叫。方向 edge 與 meta/風險（[014]）共用 strategy_calibration 但目標欄位不同。

## Complexity Tracking

> 無 Constitution 違規，免填。
