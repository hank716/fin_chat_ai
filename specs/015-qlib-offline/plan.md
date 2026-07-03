# Implementation Plan: Qlib 離線整合（隔離 image + gate 守護 serving）

**Branch**: `015-qlib-offline` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

隔離離線 image 用 Qlib Alpha158 + LightGBM 做 purged walk-forward 評估（方向 rank-IC / 風險 AUC）與
打分，落地 JSON；serving（backend）只讀 JSON、永不 import qlib，並以 rank-IC gate 守護是否採用。
容器自帶每日 off-peak 排程，與 serving 完全隔離（憲章 VI）。

## Technical Context

**Language/Version**: Python（離線 image 內，qlib/lightgbm 相依）；serving 為 Python 3.13（無 qlib）
**Primary Dependencies**: 離線：qlib、lightgbm、pandas、numpy；serving：只讀 JSON
**Storage**: `storage/strategy/qlib_eval.json`、`qlib_scores/{date}.json`、`qlib_meta.json`；Qlib dump
**Testing**: pytest（**目前缺，列為待辦**；serving gate 邏輯可測）
**Target Platform**: 獨立 Docker 服務（qlib_offline）；backend 讀檔
**Constraints**: serving 零 qlib 相依；purged walk-forward 防洩漏；gate 守門；零 LLM/外部 API
**Scale/Scope**: `qlib_offline/run.py`（343）+ `dump.py`（141）+ `common.py`（89）+ Dockerfile

## Constitution Check

*GATE: 對照憲章七原則。*

- **VI. 服務隔離** — ✅ Qlib 訓練在隔離 image、serving 永不 import qlib、以 JSON 為介面（憲章 VI、commit `271bf6f`）。
- **IV. Point-in-Time** — ✅ purged walk-forward 防未來洩漏；rank 定義對齊 [013]。
- **III. Fail-Closed（品質守門）** — ✅ 未過 rank-IC gate 的離線分數 serving 不採用。
- **II. 成本紀律** — ✅ 純離線、零 LLM/外部 API；容器自帶 off-peak 排程。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。

**結論**：通過。無違規。

## Project Structure

### Documentation (this feature)

```text
specs/015-qlib-offline/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
qlib_offline/
├── run.py         # --eval / --score / --loop：walk-forward、rank/risk 指標、打分、自帶排程
├── dump.py        # 本機 parquet → Qlib 格式
├── common.py      # 共用（路徑/常數/工具）
├── Dockerfile     # 隔離 image（qlib/lightgbm）
└── requirements.txt
backend/reports/morning_brief.py   # _apply_qlib_scores（serving，讀 JSON + gate，無 qlib import）
docker-compose.yml                 # qlib_offline 獨立服務
tests/                             # ⬜ 待新增（serving gate 邏輯）
```

**Structure Decision**: 以「檔案（JSON）為介面契約」把離線重相依與 serving 徹底解耦；離線容器自帶排程，
不與 [010] scheduler 耦合。serving 端只有一個 guarded 讀取點（`_apply_qlib_scores`）。

## Complexity Tracking

> 無 Constitution 違規，免填。
