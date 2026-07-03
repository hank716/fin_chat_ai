# Implementation Plan: 問答 — Ask（意圖分類 + 討論串記憶）

**Branch**: `008-ask-chat` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

`POST /ask` 同步流程：`check_budget` → 意圖分類（Flash-Lite, fail-open）→ 以最新晨報為 grounding、
對台股代號 on-demand 抓基本面、討論串帶記憶 → 明確快取（不相容則降級）→ `generate_text(use_search)` →
計費 `record_cost`（[009]）→ 寫記憶 + 附免責句回覆。check→spend 之間不 await 以保成本原子性。

## Technical Context

**Language/Version**: Python 3.13（FastAPI async handler，但下游為同步阻塞呼叫）
**Primary Dependencies**: FastAPI、gemini_client / gemini_cache（[005]）、tracker（[009]）、
finmind_loader / build_fundamentals、chat.history（redis）、secrets
**Storage**: redis（討論串記憶、成本桶、快取名稱）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器）；家用單 worker
**Project Type**: web-service API 端點
**Performance Goals**: 明確快取/分類/on-demand 控 token；逾限早退不呼叫貴模型
**Constraints**: check→spend 不得 await（TOCTOU）；分類器/快取/記憶/FinMind 皆須降級不阻斷
**Scale/Scope**: `backend/api/ask.py`（~185 行）+ `backend/chat/history.py`（~58 行）

## Constitution Check

*GATE: 對照憲章七原則。*

- **I. Gemini-only** — ✅ 問答與分類皆走 Gemini（[005]）。
- **II. 成本紀律** — ✅ 預算檢查 + 意圖過濾 + 明確快取；花費全計入 [009]；逾限早退。
- **III. Fail-Closed（管理端點）** — ✅ `X-Admin-Token` 未設一律不放行、`compare_digest` 防時序。
- **IV. Point-in-Time** — ✅ 以晨報/即時查詢為事實；記憶僅追問脈絡、不作事實來源。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。

**已知不變式（非違規）**：async handler 內下游同步，故 check→spend 原子；若改 async 需 redis 原子預扣。
**結論**：通過。

## Project Structure

### Documentation (this feature)

```text
specs/008-ask-chat/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/api/ask.py          # POST /ask：預算/意圖/grounding/快取/計費/記憶/免責
backend/chat/history.py     # 討論串記憶（redis list, MAX_TURNS, TTL 3d）
backend/ai/{gemini_client,gemini_cache,prompts}.py   # [005]
backend/cost/tracker.py     # [009]
backend/processor/fundamentals.py + data_sources/finmind_loader.py  # on-demand 基本面
tests/api/                  # ⬜ 待新增
```

**Structure Decision**: 維持單一 async endpoint 串接同步下游；記憶獨立成 `chat/history.py`（redis），
與問答主流程解耦，故障可獨立降級。

## Complexity Tracking

> 無 Constitution 違規，免填。
