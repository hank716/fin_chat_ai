# Tasks: 特徵運算 — 技術/籌碼/基本面/跨市場（point-in-time）

**Feature**: `003-feature-processing` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 台股技術/籌碼特徵（基線，已實作）

- [X] T001 `build_tw_features`：index/stocks/sectors/movers 組裝 `backend/processor/tw_features.py`（FR-002）
- [X] T002 `_price_block`：MA/1d·5d·20d 報酬/波動/相對大盤強弱（FR-002）
- [X] T003 `_chip_block`/`_margin_block`：三大法人淨買賣超（張）+ 連續買超天數（FR-002）
- [X] T004 `_aggregate_sectors`/`_movers`：族群聚合 + 漲跌/買賣超排行（FR-002）
- [X] T005 `_clean`：NaN→None、JSON-safe（FR-001）
- [X] T006 `build_adhoc_symbol` + `_is_stale`：清單外單檔即時查、stale 重抓（FR-008）

## Phase 2 — 基本面 / 跨市場（基線，已實作）

- [X] T007 `build_fundamentals`：月營收 + 季財報衍生指標（三率/負債比/EPS_TTM/自由現金流）+ lru + 日曆感知（FR-004, FR-005）
- [X] T008 長格式 `type/value` 寬鬆比對（精確→子字串 fallback）；資料源缺只略過該區塊（FR-006）
- [X] T009 `intermarket_features`：對齊交易日後算報酬相關性（FR-007）
- [X] T010 point-in-time：trade_date 對齊 + 未來日由 [002] 寫入閘擋（FR-003）

## Phase 3 — 測試基線（未竟，高優先）

- [ ] T011 建立 `tests/processor/`（parquet fixtures + FinMind mock）
- [ ] T012 [P] test：`build_tw_features` 型別 JSON-safe、NaN→None、籌碼單位「張」（US1 / FR-001）
- [ ] T013 [P] test：相對強弱/連買天數計算正確（US1 / FR-002）
- [ ] T014 [P] test：混入未來日列 → 特徵/as_of 不受影響（US2 / FR-003 / SC-001）
- [ ] T015 [P] test：`build_fundamentals` 衍生指標數值正確（離線）（US3 / FR-005 / SC-002）
- [ ] T016 [P] test：某資料源缺 → 略過該區塊、其餘完整（US3 / FR-006 / SC-003）
- [ ] T017 [P] test：lru + 日曆感知——同日重複查不重抓（US3 / FR-004）
- [ ] T018 test：跨市場相關性對齊交易日（US / FR-007）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T019 衍生指標與 FinMind 原始欄位對應的黃金樣本回歸測試
- [ ] T020 `/speckit-converge` 掃描 processor/ 與本 spec 落差、補未列任務

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 技術/籌碼特徵 | T001–T006, T012–T013 | 程式✅ / 測試⬜ |
| US2 point-in-time | T010, T014 | 程式✅ / 測試⬜ |
| US3 基本面衍生指標 | T007–T009, T015–T018 | 程式✅ / 測試⬜ |
