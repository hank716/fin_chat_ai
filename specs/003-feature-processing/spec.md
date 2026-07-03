# Feature Specification: 特徵運算 — 技術/籌碼/基本面/跨市場（point-in-time）

**Feature Branch**: `003-feature-processing`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: `design_docs.md` §5（核心分析維度）、§15（Market Data Processor）；憲章 I（不用 LLM 算資料）、
IV（point-in-time）；實作 `backend/processor/`（`tw_features.py`、`fundamentals.py`、
`intermarket_features.py`、`fundamentals_history.py`、`prefetch_fundamentals.py`、`market_regime.py`）；
相關 commit `07da709`（point-in-time 基本面）、`ec8c403`（未來日修復）、`e21b232`（日曆感知略過）。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。此層**只算數字、不做 AI
> 判讀**（憲章 I）；輸出純 JSON-safe dict 供 [005-ai-gemini-layer] 與 [004-guardrails] 消費。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 台股結構化特徵（技術面 + 籌碼面） (Priority: P1)

讀落地的價格/籌碼 parquet，算 index/stocks/sectors/movers：MA、1d/5d/20d 報酬、波動、相對大盤強弱、
三大法人淨買賣超（張）、連續買超天數、族群聚合、漲跌與買賣超排行。

**Why this priority**: 這是餵 AI 選股/敘事的核心結構化輸入（§5）。

**Independent Test**: `build_tw_features()` 回含 `index`/`stocks`/`sectors`/`movers` 的 dict，數字型別
JSON-safe（NaN→None），籌碼單位為「張」。

**Acceptance Scenarios**:

1. **Given** 已落地價格/籌碼 parquet，**When** `build_tw_features()`，**Then** 回結構化 dict（含相對強弱與連買天數）。
2. **Given** 某欄計算為 NaN，**When** 輸出，**Then** 該欄為 None（不外洩 NaN）。
3. **Given** 清單外標的即時查，**When** `build_adhoc_symbol(symbol)`，**Then** 抓落地後算單檔特徵（stale 則重抓）。

---

### User Story 2 - Point-in-Time 正確、無未來日洩漏 (Priority: P1)

特徵一律以「資料公布日 trade_date」對齊；不得引用未來日或幽靈列汙染 `as_of=max`。

**Why this priority**: 前視偏誤會讓回測/edge 模型績效失真（憲章 IV、commit `ec8c403`）。

**Independent Test**: 在 parquet 混入未來日列，特徵/`as_of` 不受影響（未來列被 [002] 寫入閘擋在落地前）。

**Acceptance Scenarios**:

1. **Given** 資料含近未來 glitch 列，**When** 落地/運算，**Then** 未來列不進 parquet、不影響 as_of。
2. **Given** 基本面無新一期，**When** 日曆感知略過，**Then** 不重抓（commit `e21b232`）。

---

### User Story 3 - 基本面（月營收 + 季財報）on-demand 衍生指標 (Priority: P2)

對焦點標的 on-demand 抓 FinMind 月營收 + 季財報，算衍生指標（毛利率/營益率/淨利率/負債比/EPS_TTM/
自由現金流），以 `features.tw.stocks[].fundamentals.*` 路徑暴露，過 guardrail metric/source 驗證。

**Why this priority**: 全市場 2000+ 檔不可能每天全抓（§5.2）；on-demand + process 內 lru 快取控成本。

**Independent Test**: `build_fundamentals(sym)` 回衍生指標；月更/季更當日重複查不重抓（lru）；任一資料源
失敗只略過該區塊。

**Acceptance Scenarios**:

1. **Given** 焦點標的，**When** `build_fundamentals`，**Then** 回三率/負債比/EPS_TTM/自由現金流（由原始數字推算）。
2. **Given** 某資料源缺，**When** 建構，**Then** 略過該區塊、其餘照常。

---

### Edge Cases

- 只算數字、不呼叫 LLM（憲章 I）；NaN 一律轉 None。
- 籌碼單位輸出「張」（股/1000）便於閱讀與 AI 引用。
- adhoc parquet stale 判定（`_is_stale`，max_age_days）→ 重抓再算。
- 財報長格式 `type/value` 寬鬆比對（精確→子字串 fallback）。
- 跨市場相關性以「對齊交易日後的日報酬」計（避免不同市場休市日錯位）。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 特徵層 MUST 只算數字、MUST NOT 呼叫 LLM（憲章 I）；輸出 MUST 為 JSON-safe（NaN→None）。
- **FR-002**: `build_tw_features` MUST 產出 index/stocks/sectors/movers，含技術面 + 籌碼面 + 相對強弱。
- **FR-003**: 所有時間索引 MUST 以 trade_date（公布日）對齊；MUST NOT 引入未來日或前視偏誤。
- **FR-004**: 基本面 MUST on-demand（焦點標的）+ process 內 lru 快取；日曆感知（無新一期不重抓）。
- **FR-005**: 衍生指標 MUST 由 FinMind 原始數字推算，並以 `features...fundamentals.*` 路徑暴露供 guardrail 驗證。
- **FR-006**: 任一資料源失敗 MUST 只略過該區塊，MUST NOT 讓整體特徵建構失敗。
- **FR-007**: 跨市場特徵 MUST 對齊交易日後再算報酬相關性。
- **FR-008**: adhoc 單檔特徵 MUST 支援 stale 重抓（`build_adhoc_symbol`）。

### Key Entities

- **features dict**: `tw`（index/stocks/sectors/movers）、`us`/`crypto`（intermarket）、`news`。
- **fundamentals block**: `eps_quarter/eps_ttm/gross_margin_pct/.../debt_ratio_pct/free_cashflow_ttm_100m/dividend`。
- **market_regime**: TAIFEX P/C ratio / 恐慌 gauge（[016]，被特徵/晨報消費）。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 特徵輸出無 NaN、無未來日；`as_of` 等於最新真實公布日。
- **SC-002**: 焦點標的可得三率/負債比/EPS_TTM/自由現金流；離線單元驗證數值正確。
- **SC-003**: 單一資料源缺失時，其餘特徵仍完整產出。
- **SC-004**: on-demand + lru 使基本面抓取次數維持在 FinMind 額度內（配合 [001]）。

## Assumptions

- 價格/籌碼 parquet 由 [001-data-ingestion] 落地、[002-storage-manager] 管理。
- 焦點標的 = movers / watchlist / 問答標的（見 [006]/[008]）。
- 衍生指標定義對齊 README M9 表；單位以「張」「百萬/億」等易讀口徑輸出。
