# Implementation Plan: 輸出護欄 — Symbol Guard 與六道驗證

**Branch**: `004-guardrails-symbol-guard` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

在 LLM 產出 `BriefResult` 後、報告落地前，以純函式 `run_guardrails(result, features)` 執行六道
驗證（Source/Metric、Symbol、News Citation、Advice、Intermarket Causality、Data Age），對「捏造類」
直接移除片段、對「禁語類」標記 warning，回傳清理後的 result 與可稽核 report。核心不可協商點為
**Symbol Guard fail-closed**：`features.tw.stocks` 為空時移除所有候選。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: pydantic（`ai.schemas.BriefResult`）、標準庫 `re`、`zoneinfo`
**Storage**: 無（純函式；report 由呼叫端寫入 report JSON）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器）
**Project Type**: web-service 內部模組
**Performance Goals**: 單次報告 O(片段數)，毫秒級；無外部 I/O
**Constraints**: 純函式、無副作用、不變更輸入（`model_copy(deep=True)`）
**Scale/Scope**: 單一模組 `backend/guardrails/verify.py`（~190 行）

## Constitution Check

*GATE: 對照憲章七原則。*

- **III. Guardrail Fail-Closed (NON-NEGOTIABLE)** — ✅ 本 feature 即此原則之實作；Symbol Guard 於
  stocks 空時移除全部候選（FR-003）。**核心守則，不得放寬**。
- **I. Gemini-only** — ✅ 護欄不呼叫任何 LLM，純資料比對。
- **II. 成本紀律** — ✅ 無 LLM 呼叫、無成本增量。
- **IV. Point-in-Time** — ✅ 僅消費 features 既有資料，不引入未來資料；`data_as_of` 缺失記 error。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。
- 其餘（V 儲存、VI 服務隔離）— N/A（模組層級）。

**結論**：通過。無違規，`Complexity Tracking` 免填。

## Project Structure

### Documentation (this feature)

```text
specs/004-guardrails-symbol-guard/
├── spec.md      # 現況行為規格（baseline）
├── plan.md      # 本檔
└── tasks.md     # 基線任務（已完成標記 + 未竟項）
```

### Source Code (repository root)

```text
backend/
├── guardrails/
│   ├── __init__.py
│   └── verify.py          # run_guardrails + 六道 guard + _resolve JSONPath 方言
├── ai/schemas.py          # BriefResult / Evidence / NewsDigest 定義（被消費）
└── reports/morning_brief.py:348   # 唯一呼叫端
tests/
└── guardrails/            # ⬜ 待新增：pytest 覆蓋（目前不存在）
```

**Structure Decision**: 維持單一模組 `backend/guardrails/verify.py`，以純函式對外，呼叫端只在
產報流程注入。不引入 class/狀態，維持可測試性（SC-003）。

## Complexity Tracking

> 無 Constitution 違規，免填。
