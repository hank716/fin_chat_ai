# Tasks: 市場恐慌 regime — TAIFEX P/C ratio + 恐慌 gauge

**Feature**: `016-market-regime-taifex` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — P/C ratio 資料管線（基線，已實作）

- [X] T001 `fetch_pc_recent`：OpenAPI 增量（~21 天 JSON）`backend/data_sources/taifex_loader.py`（FR-001）
- [X] T002 `fetch_pc_history`/`_parse_pc_html`：CSV 區間 2 年回填，失敗退回 OpenAPI seed（FR-001）
- [X] T003 `_upsert`/`refresh_recent`/`backfill`/`read_pcr`：trade_date upsert 落地 `_taifex/pcr.parquet`（FR-001, FR-006）
- [X] T004 對外請求過 `taifex` rate_limiter bucket（[001]）（FR-002）

## Phase 2 — 市場特徵與 gauge（基線，已實作）

- [X] T005 `pc_feature_frame`/`latest_pc_features`：一致定義 `PC_FEATURES`（訓練 map-by-date + serving）`backend/processor/market_regime.py`（FR-003）
- [X] T006 `market_fear_score`：P/C-OI z-score 全史百分位（透明、無擬合）（FR-004）
- [X] T007 市場級序列只供 regime/曝險，不當個股特徵（FR-005）

## Phase 3 — 測試基線（未竟，中優先）

- [ ] T008 建立 pcr fixtures（多日序列）+ httpx mock（OpenAPI/CSV）
- [ ] T009 [P] test：`refresh_recent` upsert 無重複列（US1 / FR-001）
- [ ] T010 [P] test：`backfill` CSV 成功；CSV 失敗退回 OpenAPI seed（US1 / FR-001）
- [ ] T011 [P] test：抓取過 rate_limiter（taifex bucket）（US1 / FR-002）
- [ ] T012 [P] test：`pc_feature_frame` 與 `latest_pc_features` 欄位/定義一致（US2 / FR-003 / SC-002）
- [ ] T013 [P] test：`market_fear_score` 百分位計算正確、可重現（US3 / FR-004 / SC-003）
- [ ] T014 test：point-in-time——asof 之後資料不影響當日 gauge（FR-006）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T015 若日後可取 VIX：加入 regime（spec Assumptions）
- [ ] T016 `/speckit-converge` 掃描 taifex_loader / market_regime 與本 spec 落差

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 P/C 資料管線 | T001–T004, T009–T011 | 程式✅ / 測試⬜ |
| US2 市場特徵一致 | T005, T012 | 程式✅ / 測試⬜ |
| US3 恐慌 gauge | T006–T007, T013–T014 | 程式✅ / 測試⬜ |
