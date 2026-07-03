# Tasks: 排程器 — 晨報/慢爬/預抓與 catch-up

**Feature**: `010-scheduler` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 晨報排程與 catch-up（基線，已實作）

- [X] T001 `_parse_times`/`_parse_hhmm`：多 HH:MM 解析、容錯、預設 `[(8,30)]` `scheduler/scheduler.py`（FR-010）
- [X] T002 `generate_brief`：HTTP `POST /brief/morning`、非交易日略過、try/except（FR-002, FR-003, FR-008）
- [X] T003 `_is_trading_day`：查 `/brief/status`，失敗保守視為交易日（FR-003）
- [X] T004 `_wait_backend_ready`：就緒輪詢 + 逾時跳過（FR-005）
- [X] T005 `catch_up`：已過最早排程 + 今日無報告 + 交易日 → 補產（FR-004）
- [X] T006 `main`：依 `REPORT_TIMES` 註冊 cron（misfire 3600 / coalesce / max_instances=1）（FR-002, FR-009）

## Phase 2 — 選用多軌慢爬/預抓（基線，已實作）

- [X] T007 `prefetch_fundamentals` + `PREFETCH_TIMES` 註冊（僅交易日）（FR-006）
- [X] T008 `crawl_fundamentals` + `CRAWL_TIMES` 註冊 + `crawl_catch_up`（FR-006, FR-007）
- [X] T009 軌道 A `crawl_listed_history_job` + `HISTORY_CRAWL_TIMES` + `history_catch_up`（不限交易日）（FR-006, FR-007）
- [X] T010 軌道 B/C 每小時慢爬 `crawl_tpex_prices_job`/`crawl_fundamentals_history_job` + `HISTORY_*_HOURLY_MIN`（非整數忽略）（FR-006, FR-010）

## Phase 3 — 測試基線（未竟，高優先）

- [ ] T011 建立 `tests/scheduler/`（httpx mock、freeze time、monkeypatch env）
- [ ] T012 [P] test：`_parse_times` 多值/去重排序/非法忽略/全空預設（FR-010）
- [ ] T013 [P] test：`generate_brief` 非交易日略過、交易日 POST（US1 / FR-003）
- [ ] T014 [P] test：`catch_up` 三分支（補產 / 已有報告 / 未到排程 / 非交易日）（US2 / FR-004）
- [ ] T015 [P] test：`crawl_catch_up`/`history_catch_up` 未設環境變數即 return；已過最早時間補觸發（US3 / FR-006–007）
- [ ] T016 [P] test：`_is_trading_day`/`/brief/status` 查詢失敗保守視為交易日（edge / FR-003）
- [ ] T017 test：觸發函式遇 httpx 例外只記 error、不拋（FR-008 / SC-002）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T018 把 job 邏輯拆到 `scheduler/jobs/` 以利測試與擴充（目前集中在 scheduler.py）
- [ ] T019 `/speckit-converge` 掃描 scheduler.py 與本 spec 落差、補未列任務

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 每日定時晨報 | T001–T002, T006, T012–T013 | 程式✅ / 測試⬜ |
| US2 catch-up 補產 | T004–T005, T014, T016 | 程式✅ / 測試⬜ |
| US3 選用多軌慢爬 | T007–T010, T015 | 程式✅ / 測試⬜ |
