# Feature Specification: Discord — 互動 bot 與晨報推播

**Feature Branch**: `011-discord-bot-notify`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §6.3、§9.2、§13.3、§21；憲章 VI（服務隔離）；
實作 `bot/bot.py`（gateway 互動）、`backend/notify/discord.py`（REST 推播）、
`backend/reports/discord_summary.py`（摘要）；相關 commit `5736b48`（討論串記憶）。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。bot 為**薄轉接層**：
> 不直接呼叫 Gemini、不碰資料；Gemini/guardrail/成本皆在 backend（[005]/[004]/[009]）。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 專屬頻道問答（權限隔離） (Priority: P1)

1 guild / 3 channels / 2 users：`jay-chat`/`hank-chat` 各自專屬，bot **只回應該頻道對應的 user**；
`daily-report` 為唯讀廣播（bot 不互動）。使用者問題 → backend `POST /ask` → 回答 + 💰 成本。

**Why this priority**: 權限隔離避免越權查詢與成本混算（§9.2/§21）。

**Independent Test**: 在 jay-chat 由 hank 發言 → 被忽略；由 jay 發言 → 轉 `/ask` 並回覆。

**Acceptance Scenarios**:

1. **Given** 訊息來自 `ALLOWED[channel]` 對應 user，**When** on_message，**Then** 轉 `/ask` 並回覆答案 + 成本行。
2. **Given** 訊息來自非對應 user 或非管理頻道，**When** on_message，**Then** 忽略。
3. **Given** 訊息在 `daily-report` 頻道（含其下討論串），**When** on_message，**Then** 不互動。

---

### User Story 2 - 討論串記憶、一般頻道無狀態 (Priority: P2)

討論串（thread）以其 parent 頻道判權限，串內問答帶 `conversation_id`（=串 id）啟用短期記憶；
一般頻道訊息維持無狀態。

**Why this priority**: 追問體驗（[008] 記憶），同時控成本/故障面。

**Independent Test**: thread 內連問，第二題可代名詞追問；一般頻道問答不帶記憶。

**Acceptance Scenarios**:

1. **Given** thread 訊息，**When** `_ask_backend`，**Then** payload 帶 `conversation_id`。
2. **Given** 一般頻道訊息，**When** `_ask_backend`，**Then** 不帶 conversation_id。

---

### User Story 3 - 每日晨報摘要 REST 推播 (Priority: P1)

backend 產報末段以 bot token 直接打 Discord REST（不需 gateway）推短摘要到 daily-report 頻道；
含成本行；失敗只記 log，不阻斷產報。

**Why this priority**: 每日主動通知（§13.3）；推播失敗不可炸掉產報流程。

**Independent Test**: `send_daily_summary(report)` 呼叫 REST；未設 token/channel → 略過回 False。

**Acceptance Scenarios**:

1. **Given** 設定 token+channel，**When** `send_daily_summary`，**Then** REST 200/201 回 True。
2. **Given** 未設 token 或 channel，**When** 推播，**Then** 記 warning、回 False、不拋。

---

### Edge Cases

- `/ask` 逾限（budget_exceeded）→ 直接回婉拒答案、不附成本行。
- `/ask` HTTP 非 200 或例外 → 回友善錯誤字串，不拋。
- 回覆截斷至 1990/1990 字（Discord 2000 限制）。
- bot 需 Discord Developer Portal 開 MESSAGE CONTENT INTENT。
- 互動 bot 用 gateway（`bot/`）；推播用 REST（`backend/notify/`）——兩條路徑分離。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: bot MUST 為薄轉接層：不呼叫 Gemini、不碰資料，一律轉 backend `/ask`。
- **FR-002**: MUST 依 `ALLOWED[channel_id]==user_id` 做頻道×使用者權限比對；不符或非管理頻道 MUST 忽略。
- **FR-003**: `daily-report` 頻道（含其下 thread）MUST NOT 互動。
- **FR-004**: thread MUST 以 parent 頻道判權限並帶 `conversation_id` 啟用記憶；一般頻道 MUST 無狀態。
- **FR-005**: 回覆 MUST 附成本行（逾限除外）並截斷至 Discord 長度限制。
- **FR-006**: `/ask` 失敗（非 200/例外）MUST 回友善字串、MUST NOT 拋。
- **FR-007**: 推播 `send_message`/`send_daily_summary` MUST 用 REST；未設 token/channel MUST 略過回 False；失敗只記 log。
- **FR-008**: 服務 MUST 隔離：互動走 gateway（bot/）、推播走 REST（backend/notify），皆獨立於核心產報（憲章 VI）。

### Key Entities

- **ALLOWED map**: `channel_id → user_id`（環境變數組成）。
- **daily summary**: `build_discord_summary(report)` 產出的短摘要（含成本）。
- **/ask 回應**: answer / cost_twd / today_spent / daily_limit / budget_exceeded（[008]）。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 只有對應 user 在其專屬頻道能取得回答；越權 100% 被忽略。
- **SC-002**: 討論串可追問；一般頻道不留狀態。
- **SC-003**: 晨報摘要每交易日推送；推播失敗不影響產報成功。
- **SC-004**: bot/推播任一失敗只降級（回友善訊息/記 log），不炸服務。

## Assumptions

- 家庭規模 1 guild / 3 channels / 2 users。
- Gemini/guardrail/成本/記憶皆在 backend（[005]/[004]/[009]/[008]）。
- bot 與 backend 同 compose 網段（`BACKEND_URL`）。
