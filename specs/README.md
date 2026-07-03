# Specs — 功能規格索引（Spec Kit）

本目錄是專案的**單一真相來源**（single source of truth）。每個 `NNN-feature/` 對應一個
使用者可感知、可獨立驗收的功能，含 `spec.md`／`plan.md`／`tasks.md`。開發受
[`.specify/memory/constitution.md`](../.specify/memory/constitution.md) 憲章約束。

`design_docs.md` 為背景/歷史規格來源；當兩者衝突，以 `specs/` 為準。

## 流程

新功能：`/speckit-specify` →（`/speckit-clarify`）→ `/speckit-plan` → `/speckit-tasks` →
（`/speckit-analyze`）→ `/speckit-implement`。既有功能採「回溯補規格」建立基線。

## 遷移對照與狀態

由 `design_docs.md` §0–§33 + README M0–M9 拆解而來。狀態：⬜ 待補 · 🟡 進行中 · ✅ 已補基線。

| NNN | Feature | 來源 §/commit | 對應路徑 | 狀態 |
|-----|---------|----------------|----------|:----:|
| 001 | data-ingestion | §14 | `backend/data_sources/` | ⬜ |
| 002 | storage-manager | §10–§12 | `backend/storage/` | ⬜ |
| 003 | feature-processing | §5, §15 | `backend/processor/` | ⬜ |
| 004 | guardrails-symbol-guard | §30 | `backend/guardrails/` | ✅ |
| 005 | ai-gemini-layer | §5, §16, §20 | `backend/ai/`, `backend/router/` | ⬜ |
| 006 | morning-brief | §6 | `backend/reports/morning_brief.py` | ⬜ |
| 007 | web-report | §7 | `backend/reports/web_renderer.py`, `templates/` | ⬜ |
| 008 | ask-chat | §20 | `backend/api/ask.py`, `backend/chat/` | ⬜ |
| 009 | cost-control | §25 | `backend/cost/` | ⬜ |
| 010 | scheduler | §13 | `scheduler/` | ⬜ |
| 011 | discord-bot-notify | §6, §20 | `bot/`, `backend/notify/` | ⬜ |
| 012 | strategy-eval-backtest | `6b6b851`,`81dc142` | `backend/reports/backtest.py`,`strategy_calibration.py` | ⬜ |
| 013 | edge-training-set | `e67f8ea` | `backend/reports/training_set.py` | ⬜ |
| 014 | meta-labeling-sizing | `2ecb82a`,`892c129` | `backend/processor/`, `backend/reports/` | ⬜ |
| 015 | qlib-offline | `271bf6f` | `qlib_offline/` | ⬜ |
| 016 | market-regime-taifex | `8772d76` | `backend/data_sources/taifex_loader.py`,`processor/market_regime.py` | ⬜ |

**回溯優先順序（風險優先）**：004 guardrails → 009 cost → 005 ai → 其餘依維護節奏補齊。

> 平台層（§9 架構、§22 Supabase schema、§26 Docker、§27 .env、§28 資料夾結構）不拆為 feature，
> 維持在 [`ARCHITECTURE.md`](../ARCHITECTURE.md) 作為 plan 階段的技術決策來源。
