# Tasks: 資料抓取 — 多來源 ingest、rate-limit、backfill

**Feature**: `001-data-ingestion` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 跨 process 節流（基線，已實作）

- [X] T001 `Quota`/`QUOTAS` per-provider 速率/burst/hourly_budget `backend/data_sources/rate_limiter.py`（FR-002）
- [X] T002 `_try_consume`：Redis WATCH/MULTI token bucket、wall-clock、防負 elapsed（FR-001, FR-004）
- [X] T003 `acquire`：阻塞取 token + jitter + `monitor.mark("data")`（FR-004, FR-009）
- [X] T004 整點配額 `_check_hourly_budget` + `RateLimitExhausted`；逾時 `RateLimitTimeout`（FR-003）
- [X] T005 Redis 故障時整點配額放行（可用性優先）（edge）

## Phase 2 — loaders 與退避（基線，已實作）

- [X] T006 FinMind loader + 402/403 退避（`FinMindQuotaExceeded`/`FinMindIPBanned`）+ 本機額度先擋（FR-005）
- [X] T007 TWSE/TPEx loader + 反爬降頻/斷路器 + `TWSENoDataError`（edge）
- [X] T008 yfinance / taifex / google_finance / news loader（各自速率）
- [X] T009 backfill_tw / backfill_tw_market（全市場走 TWSE/TPEx 分散額度）+ history_crawl 慢爬（FR-008）

## Phase 3 — ingest 編排（基線，已實作）

- [X] T010 `ingest_tw_prices/chip/daily`：每來源獨立 try（FR-006）
- [X] T011 `_fetch_with_fallback`：假日/未公布往前回退（FR-007）
- [X] T012 `_dq_filter`：`is_valid()` DQ 後落地（FR-008）

## Phase 4 — 測試基線（未竟，高優先）

- [ ] T013 建立 `tests/data_sources/`（fakeredis + httpx mock + freeze time）
- [ ] T014 [P] test：`acquire` token 足夠即回、不足阻塞、逾時 `RateLimitTimeout`（US1 / FR-002–003）
- [ ] T015 [P] test：整點配額用罄 `RateLimitExhausted`；退回增量不永久灌大計數（US1 / FR-003）
- [ ] T016 [P] test：wall-clock 補 token；時鐘回退不扣 token（FR-001, FR-004）
- [ ] T017 [P] test：FinMind 402→Quota、403→IPBanned；本機額度先擋（US2 / FR-005）
- [ ] T018 [P] test：`ingest_tw_prices` TPEx 失敗 → TWSE 仍落地、tpex 帶 error（US3 / FR-006）
- [ ] T019 [P] test：`_fetch_with_fallback` 假日回退；`_dq_filter` 濾掉 invalid 列（US3 / FR-007–008）
- [ ] T020 test：Redis 故障時 `_check_hourly_budget` 放行（edge）

## Phase 5 — 後續強化（backlog，選用）

- [ ] T021 反爬 307 斷路器狀態的可觀測指標（避免空轉假進度）
- [ ] T022 `/speckit-converge` 掃描 data_sources/ 與本 spec 落差、補未列任務

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 跨 process 節流 | T001–T005, T014–T016, T020 | 程式✅ / 測試⬜ |
| US2 配額退避止血 | T006, T017 | 程式✅ / 測試⬜ |
| US3 多來源 ingest/backfill | T009–T012, T018–T019 | 程式✅ / 測試⬜ |
