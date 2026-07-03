# Tasks: 成本控制 — 全站 AI 花費追蹤與上限

**Feature**: `009-cost-control` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 計費與上限核心（基線，已實作）

- [X] T001 定價表 `_PRICING`（pro/flash/flash-lite × small/large）+ 匯率/門檻常數，標注 latest 別名與核對日 `backend/cost/tracker.py`（FR-003）
- [X] T002 `estimate_cost_twd`：未命中 input + cached + output + tool 分別計價、級距依 prompt 總量（FR-001）
- [X] T003 `cost_of_usage`：token→TWD 唯一入口，含 grounding 邊際費（FR-002）
- [X] T004 `record_grounding_request`：當月計數 + 前 5,000 次免費（FR-006）
- [X] T005 `record_cost`：日/月桶 incrbyfloat + TTL（48h / 70d）（FR-004）
- [X] T006 `check_budget`：每月 + 每日上限檢查與婉拒原因（FR-005）
- [X] T007 `set_month_total`：校準覆寫月桶、保留/補回 TTL（FR-007）
- [X] T008 上限值由 `.env` 覆寫（`config.py`：daily=30 / monthly=600）（FR-010）

## Phase 2 — 接線（基線，已實作）

- [X] T009 `ask.py`：check→spend 原子不變式；管理放行仍計入；意圖分類成本計入（FR-009）
- [X] T010 `brief.py`：校準端點 `_require_admin`（未設 ADMIN_TOKEN → 503、compare_digest）（FR-008）
- [X] T011 `morning_brief.py`：晨報 grounded + struct 兩段成本記錄、回傳月/日累計
- [X] T012 花費顯示：`/brief/status`、首頁橫幅、`discord_summary`、`markdown_builder`

## Phase 3 — 測試基線（未竟，高優先）

> 憲章 II 要求成本模組可驗收；目前無 `tests/`。

- [ ] T013 建立 `tests/cost/`（redis 以 fakeredis 或 monkeypatch `redis_client`）
- [ ] T014 [P] test：`estimate_cost_twd` cache 折扣（input−cached 才計 input 價）（US2）
- [ ] T015 [P] test：pro >200k 套 large 級距、flash 不分級距（US2 / FR-001）
- [ ] T016 [P] test：grounding 第 5,001 次起才計 $14/1k×匯率（US2 / FR-006）
- [ ] T017 [P] test：`check_budget` 月滿/日滿/皆未滿三分支（US1 / SC-004）
- [ ] T018 [P] test：`set_month_total` 覆寫 + TTL 保留；`_get` redis 失敗回 0.0（US3 / edge）
- [ ] T019 test：校準端點未設 ADMIN_TOKEN → 503、錯 token → 401（US3 / FR-008 / SC-003）
- [ ] T020 test：`/ask` 逾限早退不發 grounded 呼叫（SC-004）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T021 latest 別名版本/費率的自動核對提醒（避免靜默低估，FR-003）
- [ ] T022 若日後下游改 async：改 redis 原子預扣（Lua/INCR）守 TOCTOU（見 spec Edge Cases）
- [ ] T023 `/speckit-converge` 掃描 tracker.py 與本 spec 落差、補未列任務

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 每日/每月上限 | T005–T006, T009, T017, T020 | 程式✅ / 測試⬜ |
| US2 精確費用換算 | T001–T004, T014–T016 | 程式✅ / 測試⬜ |
| US3 月度校準 | T007, T010, T018–T019 | 程式✅ / 測試⬜ |
