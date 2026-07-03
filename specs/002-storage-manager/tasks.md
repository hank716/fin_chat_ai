# Tasks: 儲存管理 — parquet SSOT、10GB 預算、retention、pCloud restore

**Feature**: `002-storage-manager` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — parquet SSOT 落地（基線，已實作）

- [X] T001 `write_prices/write_chip/write_margin`：per-symbol parquet、trade_date upsert（keep-last）、Decimal→float `backend/storage/local_store.py`（FR-001）
- [X] T002 未來日寫入閘 `_future_cutoff`（today + `_FUTURE_GRACE_DAYS`），擋所有來源未來列（FR-002）
- [X] T003 storage layout 子目錄對齊 §28（FR-007）

## Phase 2 — 容量監控 / retention / pCloud（基線，已實作）

- [X] T004 `local_storage_report`：footprint（st_blocks）vs budget + host disk + 整體 alert（取最嚴重）（FR-003）
- [X] T005 `enforce_retention`：`_evict_old_reports`（留最近 90 篇）+ `_prune_adhoc_parquet`（TTL、保留 watchlist+TWII）（FR-004）
- [X] T006 retention 失敗只記 log 不拋（FR-006）
- [X] T007 pCloud `backup_report` / `restore_report`（全新 root，失敗只記 log）（FR-005）

## Phase 3 — 測試基線（未竟，高優先）

- [ ] T008 建立 `tests/storage/`（tmp_path parquet + monkeypatch settings + httpx mock）
- [ ] T009 [P] test：`write_prices` 落 per-symbol；同日重寫 keep-last 無重複列（US1 / FR-001）
- [ ] T010 [P] test：trade_date > today+2 天 → 不落地（US2 / FR-002 / SC-001）
- [ ] T011 [P] test：UTC 早 1 天的當日合法列因寬限不被誤殺（US2 / edge）
- [ ] T012 [P] test：`local_storage_report` footprint≥budget→critical、host free<15GB→critical、整體取最嚴重（US3 / FR-003）
- [ ] T013 [P] test：`enforce_retention` evict 最舊報告（json+md）、保留 watchlist adhoc（US3 / FR-004）
- [ ] T014 [P] test：`restore_report` 從 pCloud 下載回本機（US3 / FR-005 / SC-003）
- [ ] T015 test：retention/pCloud 失敗不拋、不阻斷（FR-006 / SC-004）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T016 footprint 估計剩餘天數 heuristic 的實測校準（ASSUMED_DAILY_GROWTH_MB）
- [ ] T017 `/speckit-converge` 掃描 storage/ 與本 spec 落差、補未列任務

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 parquet SSOT upsert | T001, T003, T009 | 程式✅ / 測試⬜ |
| US2 未來日寫入閘 | T002, T010–T011 | 程式✅ / 測試⬜ |
| US3 預算/retention/pCloud | T004–T007, T012–T015 | 程式✅ / 測試⬜ |
