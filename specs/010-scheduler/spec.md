# Feature Specification: 排程器 — 晨報/慢爬/預抓與 catch-up

**Feature Branch**: `010-scheduler`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §13；`ARCHITECTURE.md` §M3；憲章 VI（服務隔離）；
實作 `scheduler/scheduler.py`；相關 commit `e21b232`、`0923678`、`5988a51`。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。scheduler 為**獨立服務**，
> 只透過 HTTP 觸發 backend，不重做 backend 工作（憲章 VI）。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 每日定時產生晨報（多時間點、僅交易日） (Priority: P1)

在 `REPORT_TIMES`（逗號分隔 HH:MM，預設 08:30，Asia/Taipei）以 cron 觸發 backend `POST /brief/morning`；
非台股交易日略過。

**Why this priority**: 晨報是產品主交付；定時、只在交易日產出是核心排程行為（§13）。

**Independent Test**: 設 `REPORT_TIMES=08:30`，到點觸發 `generate_brief`；`_is_trading_day()` 為 False 時略過。

**Acceptance Scenarios**:

1. **Given** 交易日到達排程點，**When** cron 觸發，**Then** `POST /brief/morning`（timeout=GENERATE_TIMEOUT）。
2. **Given** 非交易日，**When** `generate_brief(reason="scheduled")`，**Then** 略過不產。
3. **Given** `REPORT_TIMES="08:30,14:00"`，**When** 啟動，**Then** 註冊兩個 cron job。

---

### User Story 2 - 晚開機 catch-up 補產 (Priority: P1)

家用 PC 可能在排程點沒開機。啟動時：若已過今日最早排程且今日尚無報告（且為交易日），立即補產。

**Why this priority**: 沒有 always-on 觸發（ARCHITECTURE §49），catch-up 是不漏報的關鍵。

**Independent Test**: mock `/brief/status` 回 `has_today=False, is_trading_day=True` 且現在已過最早排程 → 觸發補產。

**Acceptance Scenarios**:

1. **Given** 已過最早排程、今日無報告、交易日，**When** `catch_up()`，**Then** `generate_brief(reason="catch-up")`。
2. **Given** 今日已有報告或未到排程或非交易日，**When** `catch_up()`，**Then** 不補產。
3. **Given** backend 未就緒，**When** `catch_up()`，**Then** 等待就緒（`_wait_backend_ready`），逾時則跳過。

---

### User Story 3 - 選用的慢爬/預抓多軌排程 (Priority: P2)

可選開啟：焦點基本面預抓（`PREFETCH_TIMES`）、全市場財報慢爬（`CRAWL_TIMES`，含 catch-up）、
歷史行情慢爬三軌（上市 `HISTORY_CRAWL_TIMES`、上櫃/基本面每小時 `HISTORY_*_HOURLY_MIN`）。

**Why this priority**: 分散 FinMind 用量、餵大訓練集；皆為增益，未設環境變數即關閉。

**Independent Test**: 未設 `CRAWL_TIMES` → 不註冊慢爬 job、`crawl_catch_up()` 直接 return；設了且已過最早時間 → 補觸發。

**Acceptance Scenarios**:

1. **Given** `PREFETCH_TIMES` 未設，**When** 啟動，**Then** 不註冊預抓 job。
2. **Given** `CRAWL_TIMES` 已設且已過最早時間，**When** `crawl_catch_up()`，**Then** 補觸發 `crawl_fundamentals`。
3. **Given** `HISTORY_TPEX_HOURLY_MIN` 非整數，**When** 啟動，**Then** 記 warning 並忽略該軌。

---

### Edge Cases

- 每個觸發函式包 try/except：單次失敗只記 error，MUST NOT 中止排程（`# 排程不可因單次失敗而中止`）。
- cron job：`misfire_grace_time=3600`（重啟容忍 1 小時補跑）、`coalesce=True`、`max_instances=1`。
- `_is_trading_day` / `/brief/status` 查詢失敗 → 保守視為交易日（不漏報）。
- 慢爬/歷史觸發為背景執行（backend 立即回、timeout 短），scheduler 不等待完成。
- 歷史慢爬不限交易日（歷史資料靜態）；財報慢爬季更、多為快取命中，故也不限交易日。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: scheduler MUST 為獨立服務，僅透過 HTTP 觸發 backend，MUST NOT 重做 backend 工作（憲章 VI）。
- **FR-002**: MUST 依 `REPORT_TIMES`（多 HH:MM，預設 08:30、TZ=`SCHEDULE_TZ`）註冊 cron 晨報 job；向後相容 `MORNING_REPORT_TIME`。
- **FR-003**: `generate_brief(reason="scheduled")` MUST 在非交易日略過；`_is_trading_day` 查詢失敗 MUST 保守視為交易日。
- **FR-004**: 啟動時 MUST 執行 catch-up：已過最早排程且今日無報告且交易日 → 補產。
- **FR-005**: MUST 在觸發前 `_wait_backend_ready`；逾時 MUST 跳過（不硬失敗）。
- **FR-006**: 慢爬/預抓/歷史軌 MUST 由環境變數選用開啟；未設即不註冊、對應 catch-up 直接 return。
- **FR-007**: `CRAWL_TIMES` 與 `HISTORY_CRAWL_TIMES` MUST 有啟動 catch-up（已過最早時間即補觸發一次）。
- **FR-008**: 所有觸發函式 MUST 以 try/except 包裹，單次失敗不中止排程。
- **FR-009**: cron job MUST 設 `misfire_grace_time`、`coalesce`、`max_instances=1`。
- **FR-010**: 時間解析 MUST 容錯：非法 HH:MM 記 warning 並忽略；全空回預設 `[(8,30)]`。

### Key Entities

- **REPORT_TIMES / PREFETCH_TIMES / CRAWL_TIMES / HISTORY_***: 環境變數驅動的排程時間集合。
- **BlockingScheduler（APScheduler）** + `CronTrigger`：排程引擎。
- **backend HTTP 端點**：`/brief/morning`、`/brief/prefetch`（`scope=full`）、`/brief/backfill-*`、`/brief/status`、`/health`。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 交易日在排程點（或晚開機 catch-up）可觸發晨報，無漏報。
- **SC-002**: 任一觸發/查詢失敗不使 scheduler 崩潰或停止後續排程。
- **SC-003**: 未設環境變數的慢爬/預抓軌完全不啟動（零額外 FinMind 用量）。
- **SC-004**: 重啟時 1 小時內錯過的排程點可補跑（misfire grace）。

## Assumptions

- 家用單機、backend 與 scheduler 同 compose 網段（`BACKEND_URL` 預設 `http://backend:8000`）。
- 無 always-on 外部觸發（ARCHITECTURE §49）；catch-up 取代之。
- 交易日判斷、實際產報邏輯都在 backend；scheduler 只負責「何時觸發」。
