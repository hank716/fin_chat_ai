# Implementation Plan: 資料抓取 — 多來源 ingest、rate-limit、backfill

**Branch**: `001-data-ingestion` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

抓取層移植自 finflow_ai（複製改寫）：Redis token bucket 跨 process 節流（wall-clock、jitter、整點
配額）；各 provider loader（FinMind/TWSE/TPEx/yfinance/TAIFEX/Google Finance/news）；FinMind 402/403
退避止血；`ingest.py` 每來源獨立 try + 假日回退 + DQ 過濾後落 parquet（[002]）。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: redis、httpx、pandas；`activity.monitor`（流量標記）
**Storage**: Redis（節流狀態）；parquet（落地，見 [002-storage-manager]）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器；scheduler 觸發慢爬）
**Project Type**: web-service 資料層
**Performance Goals**: 冷快取晨報 ~225 calls；FinMind 0.5/s 約 7–8 分且 < 600/h
**Constraints**: 不打爆 provider（含反爬 WAF）；跨 process 一致節流；配額耗盡退避
**Scale/Scope**: `backend/data_sources/*.py`（rate_limiter 165 / twse 633 / finmind 336 / ingest 114 …）

## Constitution Check

*GATE: 對照憲章七原則。*

- **II. 成本/額度紀律** — ✅ 跨 process 節流 + 整點配額 + 退避止血，守 FinMind 600/h 與反爬限制。
- **IV. Point-in-Time** — ✅ 落地以 trade_date 對齊、未來日由 [002] 寫入閘擋（避 future leakage）。
- **VI. 服務隔離** — ✅ 抓取在 backend；scheduler 僅觸發（[010]）。
- **I/III** — N/A（非 LLM/輸出）。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。

**已知取捨（非違規）**：Redis 故障時整點配額放行（可用性優先）；反爬靠降頻+jitter+斷路器。**結論**：通過。

## Project Structure

### Documentation (this feature)

```text
specs/001-data-ingestion/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/data_sources/
├── rate_limiter.py     # Quota/QUOTAS + acquire + _try_consume + 整點配額 + 例外
├── ingest.py           # ingest_tw_prices/chip/daily + _fetch_with_fallback + _dq_filter
├── finmind_loader.py   # FinMind + 402/403 退避（FinMindBackoff/Quota/IPBanned）
├── twse_loader.py      # TWSE/TPEx daily + 籌碼 + 反爬處理
├── backfill_tw.py / backfill_tw_market.py   # 歷史/全市場回補（走 TWSE/TPEx 分散額度）
├── history_crawl.py    # 歷史行情慢爬（斷路器、降頻）
├── google_finance_loader.py / taifex_loader.py / news_loader.py / yfinance_loader.py
tests/data_sources/     # ⬜ 待新增
```

**Structure Decision**: per-provider loader + 單一共享 rate_limiter；ingest 以「每來源獨立 try」隔離
失敗。落地委派給 [002]，抓取層只負責取得與正規化。

## Complexity Tracking

> 無 Constitution 違規，免填。
