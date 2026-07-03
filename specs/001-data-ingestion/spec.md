# Feature Specification: 資料抓取 — 多來源 ingest、rate-limit、backfill

**Feature Branch**: `001-data-ingestion`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §14（Data Sources）；憲章 II（成本/額度紀律）、IV（point-in-time）、
VI（服務隔離）；實作 `backend/data_sources/`（`rate_limiter.py`、`ingest.py`、`finmind_loader.py`、
`twse_loader.py`、`backfill_tw*.py`、`history_crawl.py`、`google_finance_loader.py`、`taifex_loader.py`、
`news_loader.py`、`yfinance_loader.py`）；相關 commit `b8a705e`、`1f82cfc`、`5988a51`。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。抓取層移植自姊妹專案
> finflow_ai（複製改寫、非 import），落地改為 parquet（見 [002-storage-manager]）。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 跨 process 一致的對外 API 節流 (Priority: P1)

worker/scheduler/api 為不同 process，共享 Redis token bucket 才能正確 throttle 整個系統對每個
provider（finmind/twse/tpex/yahoo/taifex/google_finance）的用量，避免打爆被封 IP。

**Why this priority**: FinMind 免費 600/h、TWSE/TPEx 有反爬 WAF；節流失效會導致封鎖、晨報失敗。

**Independent Test**: 多次 `acquire("finmind")` 消耗 token；桶空時阻塞至補滿或 `RateLimitTimeout`；
`last` 時間戳用 wall-clock（跨 process 一致）。

**Acceptance Scenarios**:

1. **Given** token 足夠，**When** `acquire(provider)`，**Then** 立即回、消耗 cost 個 token 並 `monitor.mark("data")`。
2. **Given** token 不足且超過 `max_wait_sec`，**When** `acquire`，**Then** 抛 `RateLimitTimeout`。
3. **Given** 當小時 `hourly_budget` 用罄，**When** `acquire`，**Then** 抛 `RateLimitExhausted`（呼叫端應退避非重試）。

---

### User Story 2 - provider 配額耗盡改退避止血 (Priority: P1)

FinMind 402（每小時額度）/403（IP 封鎖）時，MUST 退避（`FinMindBackoff`：`FinMindQuotaExceeded`/
`FinMindIPBanned`），不再硬掃重試；本機端小時額度用罄時先擋下（免得再打一筆失敗請求）。

**Why this priority**: 硬掃重試會延長封鎖、浪費額度（commit `1f82cfc`）。

**Independent Test**: mock FinMind 回 402 → raise `FinMindQuotaExceeded`；回 403 → `FinMindIPBanned`；
呼叫端（如問答基本面、慢爬）逐檔 except 接住、續跑或略過。

**Acceptance Scenarios**:

1. **Given** FinMind 回 402，**When** loader，**Then** raise `FinMindQuotaExceeded`（含約整點回補語意）。
2. **Given** 本機小時額度已用罄，**When** loader，**Then** 先擋（不對外送）。

---

### User Story 3 - 多來源 ingest 與 backfill，單來源失敗不阻斷 (Priority: P2)

每日刷新（TWSE MI_INDEX + TPEx daily + T86/3itrade 籌碼）與歷史 backfill 落 parquet；每個來源獨立
try，單一來源失敗不影響其他；假日/未公布往前回退（`_fetch_with_fallback`）；DQ 過 `is_valid()` 才落地。

**Why this priority**: 資料完整性與韌性；全市場初始化走 TWSE/TPEx（不打 FinMind）分散額度。

**Independent Test**: `ingest_tw_prices` 令 TPEx 拋例外 → TWSE 仍落地、result.sources.tpex 帶 error。

**Acceptance Scenarios**:

1. **Given** start 為假日，**When** `_fetch_with_fallback`，**Then** 往前回退最多 7 天找到有資料日。
2. **Given** TPEx 抓取失敗，**When** `ingest_tw_prices`，**Then** TWSE 結果仍返回、TPEx 記 error 不中斷。
3. **Given** 抓回列，**When** 落地，**Then** 僅 `is_valid()` 通過者寫入。

---

### Edge Cases

- TWSE/TPEx 反爬 307：降頻（0.4/s、小 burst）+ `acquire` 加 jitter 打散規律高頻（避 WAF pattern）；
  斷路器避免空轉假進度（commit `5988a51`）。
- token 補充用 `max(0, now-last)`（防 NTP 校時/時鐘回退扣 token）。
- Redis 故障時 `_check_hourly_budget` 放行（寧可少擋，不因快取層掛掉擋住所有請求）。
- Google Finance / TAIFEX 為爬取/公開資料，量小、用最保守速率；Google Finance 不設 hourly_budget。
- TPEx daily endpoint 永遠回今日（不支援歷史）。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: rate limiter MUST 以 Redis token bucket 跨 process 共享；`last` 時間戳 MUST 用 wall-clock。
- **FR-002**: 每 provider MUST 有 `Quota(rate_per_sec, burst, hourly_budget)`；`acquire` MUST 支援 cost 與 `max_wait_sec`。
- **FR-003**: 整點配額用罄 MUST 抛 `RateLimitExhausted`；瞬時等待逾時 MUST 抛 `RateLimitTimeout`。
- **FR-004**: `acquire` 等待 MUST 加 jitter 打散規律高頻；token 補充 MUST 防負 elapsed。
- **FR-005**: FinMind 402/403 MUST 轉 `FinMindQuotaExceeded`/`FinMindIPBanned` 退避，MUST NOT 硬掃重試；本機額度用罄 MUST 先擋。
- **FR-006**: ingest MUST 每來源獨立 try，單一來源失敗 MUST NOT 阻斷其他來源。
- **FR-007**: 假日/未公布 MUST 以 `_fetch_with_fallback` 往前回退（上限 `max_back`）。
- **FR-008**: 落地前 MUST 過 DQ（`is_valid()`）；全市場初始化 MUST 走 TWSE/TPEx（不打 FinMind）分散額度。
- **FR-009**: 抓取 MUST `monitor.mark("data")` 記對外流量（待機偵測用）。

### Key Entities

- **Quota / token bucket**: `finchat:ratelimit:{provider}` + `:h{epoch_hour}` 整點桶。
- **loaders**: finmind / twse(含 tpex) / yfinance / taifex / google_finance / news。
- **PriceRow / ChipRow**: 正規化列（含 `is_valid()` DQ）。
- **例外**: `RateLimitTimeout`、`RateLimitExhausted`、`FinMindBackoff`（Quota/IPBanned）、`TWSENoDataError`。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 多 process 同時抓取時，對每 provider 的整體用量不超過其 per-hour 配額（不被封）。
- **SC-002**: provider 配額耗盡時退避止血，不產生持續失敗重試風暴。
- **SC-003**: 單一來源失敗時，其他來源資料仍成功落地。
- **SC-004**: 落地資料 100% 通過 DQ 且無未來日（配合 [002] 寫入閘）。

## Assumptions

- Redis 為跨 process 共享節流狀態儲存。
- 抓取層移植 finflow_ai（key prefix 改 `finchat` 避免撞 key）。
- 全市場 2700+ 檔採結構面分散（backfill 走 TWSE/TPEx）+ 慢爬 reentrant（見 [010-scheduler]）。
