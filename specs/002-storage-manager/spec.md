# Feature Specification: 儲存管理 — parquet SSOT、10GB 預算、retention、pCloud restore

**Feature Branch**: `002-storage-manager`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §9.3、§10（10GB）、§11（pCloud restore）、§12（Storage Manager）、§28；
憲章 V（local-first 10GB + pCloud）、IV（point-in-time 未來日閘）；實作 `backend/storage/`
（`local_store.py`、`storage_monitor.py`、`retention.py`）+ `backend/publish/pcloud_backup.py`；
相關 commit `ec8c403`（未來日修復）。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。本機 parquet 為 SSOT；
> Postgres 不使用（見 [[fin-chat-ai-project]]）。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - parquet SSOT 落地（per-symbol upsert） (Priority: P1)

正規化後的 PriceRow/ChipRow/MarginRow 寫進 per-symbol parquet，依 trade_date upsert（重抓同日覆蓋），
時間索引一律用公布日 trade_date。

**Why this priority**: 這是全系統的單一真相來源（§9.3）；upsert 正確性影響所有下游特徵。

**Independent Test**: `write_prices(rows, "tw")` 落 `local_parquet/tw/{symbol}.parquet`；同日重寫覆蓋舊值、
不重複列。

**Acceptance Scenarios**:

1. **Given** 一批 PriceRow，**When** `write_prices`，**Then** 落 per-symbol parquet、Decimal→float。
2. **Given** 同一 trade_date 重抓，**When** 再 `write_prices`，**Then** keep-last 覆蓋、無重複列。

---

### User Story 2 - 未來日寫入閘（防幽靈列） (Priority: P1)

任何來源的未來日列 MUST 在落地前被擋，否則 upsert keep-last 會讓未來列永久汙染 `as_of=max`。

**Why this priority**: 前視偏誤根因修復（憲章 IV、commit `ec8c403`）；這是跨所有來源的最後一道閘。

**Independent Test**: 傳入 trade_date 晚於 `today + _FUTURE_GRACE_DAYS` 的列 → 不寫入 parquet。

**Acceptance Scenarios**:

1. **Given** 列 trade_date > today+2 天，**When** 落地，**Then** 該列被擋、不進 parquet。
2. **Given** 容器 UTC 比台北早 1 天的當日合法列，**When** 落地，**Then** 因 2 天寬限不被誤殺。

---

### User Story 3 - 10GB 預算監控 + retention 清理 + pCloud 回補 (Priority: P2)

監控 footprint（storage 子目錄實際佔用，用 st_blocks）vs `LOCAL_STORAGE_BUDGET_GB` 與主機磁碟；
retention 清舊報告（本機留最近 90 篇，其餘已備份 pCloud）與過期 adhoc parquet；查看舊報告時 pCloud 回補。

**Why this priority**: 守 local-first 10GB 上限（憲章 V）；舊資料可回補故可安全清理。

**Independent Test**: `local_storage_report()` 回 footprint/host 雙視角與 alert_level；`enforce_retention()`
清舊報告與 adhoc parquet；`restore_report(rid)` 從 pCloud 下載回本機。

**Acceptance Scenarios**:

1. **Given** footprint ≥ budget，**When** `local_storage_report`，**Then** `footprint_alert_level="critical"`，整體取兩視角最嚴重。
2. **Given** 本機報告 > 90 篇，**When** `enforce_retention`，**Then** evict 最舊者（json+md），保留 watchlist adhoc parquet。
3. **Given** 本機無該報告，**When** `restore_report(rid)`，**Then** 從 pCloud 下載回 reports 目錄。

---

### Edge Cases

- footprint 用 `st_blocks×512`（實際磁碟佔用，同 `du`），非 st_size（大量小檔 block rounding）。
- 主機磁碟：< 15GB free critical / < 30GB free warning；footprint：≥100% critical / ≥70% warning。
- retention 失敗只記 log、不丟例外（不阻斷產報）。
- adhoc parquet 清理保留 watchlist 標的 + TWII；超過 `ADHOC_PARQUET_TTL_DAYS` 未更新才刪。
- pCloud 備份/回補失敗只記 log，不阻斷主流程；用全新 root `/AI-Market-Research` 避免與 finflow 撞。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: MUST 以 per-symbol parquet 為 SSOT，依 trade_date upsert（keep-last），Decimal→float。
- **FR-002**: 落地 MUST 過未來日寫入閘（trade_date ≤ today + `_FUTURE_GRACE_DAYS`），擋所有來源的未來列。
- **FR-003**: 容量監控 MUST 用 st_blocks 計 footprint，回 footprint vs budget 與 host disk 雙視角 + 整體 alert（取最嚴重）。
- **FR-004**: retention MUST 本機保留最近 `KEEP_REPORTS_LOCAL` 篇報告、清除逾 `ADHOC_PARQUET_TTL_DAYS` 的 adhoc parquet（保留 watchlist+TWII）。
- **FR-005**: MUST 支援 pCloud 冷備份（`backup_report`）與 on-demand 回補（`restore_report`）；失敗只記 log。
- **FR-006**: retention/pCloud/監控任一失敗 MUST NOT 阻斷產報主流程。
- **FR-007**: storage footprint 子目錄 MUST 對齊 §28 layout（local_parquet/features/reports/raw/cache/logs）。

### Key Entities

- **parquet SSOT**: `local_parquet/{market}/{symbol}.parquet`（價）、`/_chip/`、`/_margin/`。
- **storage report**: footprint（used/budget/pct/components）+ host disk + alert_level + 估計剩餘天數。
- **pCloud backup**: `{PCLOUD_REMOTE_ROOT}/backups/reports/`（json+md）。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 未來日列 100% 進不了 parquet；`as_of` 永遠 ≤ 今日。
- **SC-002**: 本機 footprint 維持在 `LOCAL_STORAGE_BUDGET_GB` 內（retention 生效）。
- **SC-003**: 已清理的舊報告可由 pCloud 完整回補。
- **SC-004**: 監控/清理/備份失敗時，晨報產出不受影響。

## Assumptions

- 本機 parquet 為 SSOT，無 Postgres（[[fin-chat-ai-project]]）。
- pCloud 沿用 finflow 憑證但用全新 root，避免衝突。
- 落地資料由 [001-data-ingestion] 提供；特徵由 [003-feature-processing] 讀取。
- budget 預設對齊 design_docs §10 的 10GB（`LOCAL_STORAGE_BUDGET_GB` 可調）。
