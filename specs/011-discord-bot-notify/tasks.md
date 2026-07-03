# Tasks: Discord — 互動 bot 與晨報推播

**Feature**: `011-discord-bot-notify` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 互動 bot（基線，已實作）

- [X] T001 `bot.py` gateway + intents（MESSAGE CONTENT）+ `ALLOWED` map 組裝 `bot/bot.py`（FR-002）
- [X] T002 `on_message`：頻道×使用者權限比對、daily 廣播不互動（FR-002, FR-003）
- [X] T003 thread parent 判權限 + `conversation_id` 記憶；一般頻道無狀態（FR-004）
- [X] T004 `_ask_backend`：轉 `/ask`、成本行、逾限不附成本、錯誤回友善字串、截斷 1990（FR-001, FR-005, FR-006）

## Phase 2 — 晨報推播（基線，已實作）

- [X] T005 `send_message`：REST 發訊、未設 token/channel 略過、失敗只記 log `backend/notify/discord.py`（FR-007）
- [X] T006 `send_daily_summary` + `build_discord_summary`（短摘要 + 成本）（FR-007）
- [X] T007 服務隔離：gateway（bot/）與 REST（notify）分離（FR-008）

## Phase 3 — 測試基線（未竟，中優先）

- [ ] T008 建立 `tests/bot/`、`tests/notify/`（mock discord.Message + httpx mock）
- [ ] T009 [P] test：對應 user 在專屬頻道 → 轉 /ask；越權/他人 → 忽略（US1 / FR-002）
- [ ] T010 [P] test：daily-report 頻道（含 thread）不互動（US1 / FR-003）
- [ ] T011 [P] test：thread 帶 conversation_id；一般頻道不帶（US2 / FR-004）
- [ ] T012 [P] test：`_ask_backend` 非 200/例外 → 友善字串；逾限不附成本行（US1 / FR-006）
- [ ] T013 [P] test：`send_message` 未設 token/channel → False；HTTP 失敗只記 log（US3 / FR-007）
- [ ] T014 test：回覆截斷至 Discord 長度限制（FR-005）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T015 把 bot 指令拆到 `bot/commands/`（目前集中在 bot.py）
- [ ] T016 `/speckit-converge` 掃描 bot.py / notify 與本 spec 落差

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 專屬頻道問答 | T001–T002, T004, T009–T010, T012 | 程式✅ / 測試⬜ |
| US2 討論串記憶 | T003, T011 | 程式✅ / 測試⬜ |
| US3 晨報推播 | T005–T006, T013 | 程式✅ / 測試⬜ |
