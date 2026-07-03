# Implementation Plan: 成本控制 — 全站 AI 花費追蹤與上限

**Branch**: `009-cost-control` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

以 redis 日/月桶累進全站 Gemini 花費；`cost_of_usage` 為 token→TWD 的唯一入口，涵蓋 cache 折扣、
Pro >200k 級距、Google 搜尋 grounding 免費額；查詢前 `check_budget` 守每日/每月上限，逾限婉拒；
管理者可用後台實際金額 `set_month_total` 校準，端點以 `ADMIN_TOKEN` fail-closed 保護。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: redis（`redis_client`）、標準庫 `datetime`/`zoneinfo`/`secrets`
**Storage**: redis buckets（`cost:day:*` 48h、`cost:month:*` 70d、`cost:grounding:*`）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器）；家用單 worker
**Project Type**: web-service 內部模組 + FastAPI 端點
**Performance Goals**: O(1) redis 操作；不阻塞主流程
**Constraints**: check→spend 之間不得 await（TOCTOU 不變式）；redis 失敗須降級不擋主流程
**Scale/Scope**: `backend/cost/tracker.py`（~200 行）+ 呼叫端接線

## Constitution Check

*GATE: 對照憲章七原則。*

- **II. 成本紀律** — ✅ 本 feature 即此原則之實作（每日 NT$30 / 每月 NT$600、cache 命中不重複呼叫、
  預算檢查、成本校準）。**核心守則**。
- **I. Gemini-only** — ✅ 單一費率表涵蓋 serving 成本；無其他 LLM。
- **III. Fail-Closed（延伸）** — ✅ 校準/管理端點未設 `ADMIN_TOKEN` 即停用（503），`compare_digest` 防時序。
- **IV. Point-in-Time** — N/A。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。

**已知風險（非違規，記錄之）**：跨 process 為軟上限、超支有界；`*-latest` 別名改指時費率表需人工同步
（FR-003）。**結論**：通過。

## Project Structure

### Documentation (this feature)

```text
specs/009-cost-control/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/
├── cost/
│   ├── __init__.py
│   └── tracker.py         # 定價表 + estimate/cost_of_usage + record_cost + check_budget + set_month_total
├── config.py              # daily/monthly_cost_limit_twd、admin_token（.env 可覆寫）
├── ai/gemini_client.py    # _usage_of：usageMetadata → 計費欄位
├── api/
│   ├── ask.py             # check→spend 不變式；管理放行；意圖分類成本
│   └── brief.py           # /brief/status 顯示花費；校準端點（_require_admin）
└── reports/{morning_brief,discord_summary,markdown_builder}.py  # 記錄/顯示花費
tests/
└── cost/                  # ⬜ 待新增：pytest 覆蓋（目前不存在）
```

**Structure Decision**: 維持單一模組 `tracker.py` 作為計費/上限中樞，對外皆為函式；redis 為唯一
狀態儲存。不引入資料庫或背景 worker，維持家用單機的簡單性。

## Complexity Tracking

> 無 Constitution 違規，免填。
