# Implementation Plan: 每日市場晨報

**Branch**: `006-morning-brief` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

`generate_morning_brief` 為晨報編排器：組合 features → 注入策略校準 → 兩段式 grounded LLM（[005]）→
記錄成本（[009]）→ `run_guardrails` 清理（[004]）→ 本機打分層/回測迴圈（全 guarded）→ 組 report dict →
落地 JSON/MD/copy-for-AI → 選用 Discord 推送與 pCloud/Supabase 發布 + retention。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: 內部模組（gemini_client、tracker、run_guardrails、builders、strategy_calibration、
各打分 `_apply_*`）；pathlib；zoneinfo
**Storage**: 本機 `storage/reports/*.json|.md`（落地）；pCloud/Supabase（選用發布）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器）
**Project Type**: web-service（編排流程 + 讀取 API）
**Performance Goals**: 單篇約 NT$8、數分鐘內完成
**Constraints**: 打分/回測不得阻斷核心產報；report_id 路徑守衛
**Scale/Scope**: `backend/reports/morning_brief.py`（~516 行，含讀取 API）

## Constitution Check

*GATE: 對照憲章七原則。*

- **I. Gemini-only** — ✅ 唯一 LLM 呼叫走 [005]。
- **II. 成本紀律** — ✅ 兩段成本計入 [009]；貴模型僅用於研究段。
- **III. Fail-Closed** — ✅ 落地前必過 `run_guardrails`（[004]）。
- **IV. Point-in-Time** — ✅ 消費 [003] 的 point-in-time features；回測迴圈用歷史回放。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。

**穩健性設計（非違規）**：打分/回測/publish 全包 try/except，錯誤不外溢。**結論**：通過。

## Project Structure

### Documentation (this feature)

```text
specs/006-morning-brief/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/reports/
├── morning_brief.py       # generate_morning_brief 編排 + 讀取 API（load/list/latest）
├── markdown_builder.py     # build_markdown / build_copy_for_ai / build_report_dict
├── strategy_calibration.py # build_calibration_block（注入研究段）
└── backtest.py             # _run_backtest_loop（本機回測，零 LLM）
backend/{ai,cost,guardrails}/  # [005]/[009]/[004] 相依
backend/{notify,publish,storage}/ # Discord / pCloud+Supabase / retention（選用旁路）
tests/reports/              # ⬜ 待新增
```

**Structure Decision**: 維持單一編排函式 + 一系列 guarded `_apply_*` 打分步驟；打分/回測與核心產報
解耦（try/except 隔離），確保晨報產出的高可用。

## Complexity Tracking

> 無 Constitution 違規，免填。
