# Implementation Plan: Discord — 互動 bot 與晨報推播

**Branch**: `011-discord-bot-notify` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

兩條分離路徑：①互動 bot（`bot/`，discord.py gateway）以頻道×使用者權限轉 backend `/ask`，thread 帶
記憶；②晨報推播（`backend/notify/discord.py`，REST）在產報末段推短摘要。bot 為薄轉接層，所有 AI/
成本/護欄在 backend；任一失敗只降級。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: discord.py（gateway）、httpx（/ask 與 REST 推播）
**Storage**: 無（狀態在 backend；記憶在 [008] 的 redis）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux container（bot 獨立服務；notify 在 backend）
**Project Type**: 互動 worker（bot）+ web-service 旁路（notify）
**Constraints**: 薄轉接、權限隔離、失敗降級、Discord 2000 字限制、MESSAGE CONTENT INTENT
**Scale/Scope**: `bot/bot.py`（116）+ `backend/notify/discord.py`（49）+ `discord_summary.py`（116）

## Constitution Check

*GATE: 對照憲章七原則。*

- **VI. 服務隔離** — ✅ 互動（gateway）與推播（REST）分離，皆獨立於核心產報；bot 不含業務邏輯。
- **II. 成本紀律** — ✅ 成本在 backend 計；bot 只顯示成本行。
- **III. Fail-Closed（延伸）** — ✅ 權限比對嚴格（頻道×使用者），越權忽略；逾限婉拒不附成本。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。
- I/IV/V — N/A（bot 不呼叫 LLM/不碰資料/不管儲存）。

**結論**：通過。無違規。

## Project Structure

### Documentation (this feature)

```text
specs/011-discord-bot-notify/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
bot/
├── bot.py              # gateway：on_message 權限比對 + _ask_backend + thread 記憶
└── commands/           # （目前僅 __init__.py）
backend/notify/discord.py          # REST send_message / send_daily_summary
backend/reports/discord_summary.py # build_discord_summary（短摘要 + 成本）
tests/bot/ , tests/notify/         # ⬜ 待新增
```

**Structure Decision**: 互動與推播刻意分兩服務/模組（gateway vs REST），避免耦合；bot 保持薄，
所有智慧在 backend，符合服務隔離與失敗降級。

## Complexity Tracking

> 無 Constitution 違規，免填。
