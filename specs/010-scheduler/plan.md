# Implementation Plan: 排程器 — 晨報/慢爬/預抓與 catch-up

**Branch**: `010-scheduler` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

獨立 scheduler 服務以 APScheduler `BlockingScheduler` + `CronTrigger` 在 `SCHEDULE_TZ` 定時 HTTP 觸發
backend：晨報（`REPORT_TIMES`，僅交易日）、選用的預抓/全市場財報慢爬/歷史三軌慢爬。啟動時做
catch-up（晨報 + 財報慢爬 + 上市歷史）補跑晚開機錯過的工作。所有觸發包 try/except，單次失敗不中止排程。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: APScheduler（Blocking + CronTrigger）、httpx、zoneinfo
**Storage**: 無（狀態在 backend；scheduler 無持久狀態）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux container（獨立服務，docker-compose）
**Project Type**: 排程 worker（無對外埠）
**Performance Goals**: 觸發即返回；慢爬走背景（backend 立即回、短 timeout）
**Constraints**: 不重做 backend 工作；單次失敗不中止；misfire 1h 補跑
**Scale/Scope**: `scheduler/scheduler.py`（~327 行）

## Constitution Check

*GATE: 對照憲章七原則。*

- **VI. 服務隔離** — ✅ scheduler 為獨立 compose 服務，僅 HTTP 觸發 backend，不含業務邏輯（憲章 VI、ARCHITECTURE §M3）。
- **II. 成本紀律** — ✅ 慢爬/預抓皆選用（未設環境變數即關），分散 FinMind 用量；晨報成本在 backend 計。
- **IV. Point-in-Time** — N/A（觸發層）。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。
- I/III/V — N/A（scheduler 不呼叫 LLM、不做輸出、不管儲存）。

**穩健性設計（非違規）**：查詢失敗保守視為交易日（不漏報）；觸發失敗只記 error。**結論**：通過。

## Project Structure

### Documentation (this feature)

```text
specs/010-scheduler/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
scheduler/
├── scheduler.py       # main() 註冊 cron + 各觸發函式 + catch-up 系列 + _wait_backend_ready
├── jobs/              # （目前僅 __init__.py，job 邏輯集中在 scheduler.py）
├── Dockerfile
└── requirements.txt
# 觸發的 backend 端點（另屬 006/001 等 feature）：
#   /brief/morning /brief/prefetch(?scope=full) /brief/backfill-* /brief/status /health
tests/scheduler/       # ⬜ 待新增
```

**Structure Decision**: 邏輯集中於單一 `scheduler.py`（觸發函式 + catch-up + 時間解析），以環境變數
驅動多軌排程；`jobs/` 目錄保留供日後拆分。維持「純觸發、零業務」以符合服務隔離。

## Complexity Tracking

> 無 Constitution 違規，免填。
