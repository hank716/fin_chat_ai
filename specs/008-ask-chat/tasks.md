# Tasks: 問答 — Ask（意圖分類 + 討論串記憶）

**Feature**: `008-ask-chat` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 問答核心（基線，已實作）

- [X] T001 `POST /ask` handler + `AskRequest`/`AskResponse` `backend/api/ask.py`
- [X] T002 `check_budget` + 逾限婉拒（`budget_exceeded=True`）（FR-001）
- [X] T003 管理放行 `_admin_ok`（compare_digest、未設 token 不放行、花費仍計入）（FR-002）
- [X] T004 意圖分類 fail-open + 非財務題早退（FR-003）
- [X] T005 以 `latest_report_id`/`load_report` 為 grounding；無晨報 404（FR-004）
- [X] T006 on-demand 台股基本面（`_TW_CODE`、前 3 檔）+ FinMind 退避略過（FR-005）
- [X] T007 static/variable block + 明確快取；400 降級完整 prompt（FR-006）
- [X] T008 計費 `cost_of_usage(grounded=True)` + `record_cost`；check→spend 不 await（FR-008）
- [X] T009 Gemini 429/其他錯誤 → 503（FR-010）
- [X] T010 免責句附加 + 記憶存乾淨答案（FR-009）

## Phase 2 — 討論串記憶（基線，已實作）

- [X] T011 `chat/history.py`：`load`/`append`（redis list、MAX_TURNS=6、截斷、TTL 3 天）（FR-007）
- [X] T012 一般頻道（無 conversation_id）維持無狀態

## Phase 3 — 測試基線（未竟，高優先）

- [ ] T013 建立 `tests/api/`（FastAPI TestClient + monkeypatch tracker/gemini/redis）
- [ ] T014 [P] test：逾限無權杖 → 婉拒 `budget_exceeded`；有權杖 → 放行且計費（US1 / FR-001–002）
- [ ] T015 [P] test：非財務題早退僅計分類成本；分類器失敗 fail-open（US2 / FR-003）
- [ ] T016 [P] test：無晨報 → 404（US3 / FR-004）
- [ ] T017 [P] test：明確快取 400 → 降級完整 prompt（US3 / FR-006）
- [ ] T018 [P] test：FinMind 退避 → 缺基本面但照常回答（US3 / FR-005）
- [ ] T019 [P] test：`history.load/append` 截斷、限輪數、TTL；讀寫失敗回空不拋（FR-007）
- [ ] T020 test：記憶存乾淨答案、回覆含免責句（FR-009）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T021 若下游改 async：redis 原子預扣守 TOCTOU（見 [009]）
- [ ] T022 `/speckit-converge` 掃描 ask.py / history.py 與本 spec 落差

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 預算/管理放行 | T002–T003, T008, T014 | 程式✅ / 測試⬜ |
| US2 意圖過濾 | T004, T015 | 程式✅ / 測試⬜ |
| US3 grounding/記憶/降級 | T005–T007, T010–T012, T016–T020 | 程式✅ / 測試⬜ |
