# Feature Specification: Edge 模型 — 歷史回放訓練集 + per-horizon 方向 edge

**Feature Branch**: `013-edge-training-set`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: 憲章 IV（無未來洩漏）、II（零 LLM 成本）；實作 `backend/reports/training_set.py`、
`backend/reports/strategy_calibration.py`（edge/rank 模型訓練部分）、`backend/reports/morning_brief.py`
（`_apply_edge_scores`/`_apply_rank_scores`）；相依 [012]（scorecard/live 樣本）；相關 commit `e67f8ea`、
`c8e4e85`、`71d9a12`。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。方向 edge 為方案 B：在歷史
> 上回放線上選股規則，瞬間產出與線上同分布的訓練集，訓練 per-horizon 方向模型替候選重排。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 歷史回放產生同分布訓練集（無未來洩漏） (Priority: P1)

對 parquet 每個交易日，用與線上完全相同的 movers 排行選出「模擬歷史選股」，配上當天 point-in-time
特徵與往後 5/20 日真實漲跌標籤，瞬間產出數千筆樣本。

**Why this priority**: 線上樣本累積要好幾週；回放讓 edge 模型第 1 週就有足夠訓練資料（commit `e67f8ea`）。

**Independent Test**: `build_training_set(hs)` 產出樣本表；特徵/排行只用 `trade_date ≤ D`、標籤只用 `> D`。

**Acceptance Scenarios**:

1. **Given** 歷史 parquet，**When** `build_training_set`，**Then** 每交易日回放選股 + point-in-time 特徵 + 未來標籤。
2. **Given** 特徵計算，**When** 產樣本，**Then** 只用 `≤ D` 資料（shift/rolling）；標籤用 `close.shift(-h)`（`> D`）。
3. **Given** 樣本重疊，**When** 訓練，**Then** `_overlap_weights` 給重疊窗降權。

---

### User Story 2 - per-horizon 方向 edge 模型 + walk-forward OOS (Priority: P1)

用回放 + 線上樣本訓練 HistGradientBoosting，時間序 walk-forward 切分算 OOS 指標；替當日候選打「成功
機率」供 [006] 重排（未過 gate 則不動）。

**Why this priority**: 把方向訊號轉成可重排的機率；walk-forward 防過擬合。

**Independent Test**: `train_edge_model()` 回模型 + OOS 指標（含 precision@k、校準器）；樣本不足自動跳過。

**Acceptance Scenarios**:

1. **Given** 足夠樣本，**When** `train_edge_model`，**Then** 訓練 per-horizon 模型並落地、回 OOS 指標。
2. **Given** 樣本不足，**When** 訓練，**Then** 自動跳過、退回純文字校準（[012]）。
3. **Given** 有模型且過 gate，**When** `_apply_edge_scores`/`_apply_rank_scores`，**Then** 打分並重排偏多候選。

---

### User Story 3 - guarded serving（無模型/未過 gate 不動） (Priority: P2)

打分/重排為 guarded：無離線模型檔或未過 rank-IC gate 時不重排，且任何例外只記 warning、不影響晨報。

**Why this priority**: 穩健性——附加分析不可拖垮晨報（憲章 III 的穩健延伸）。

**Independent Test**: 無模型檔時 `_apply_edge_scores` 為 no-op；拋例外時 [006] 記 warning 續行。

**Acceptance Scenarios**:

1. **Given** 無模型檔，**When** `_apply_edge_scores`，**Then** 不重排（guarded no-op）。

---

### Edge Cases

- 三律防洩漏：特徵/排行 `≤ D`、標籤 `> D`、訓練端 walk-forward 切分。
- 對齊線上分布：`MIN_AMOUNT_TWD` 流動性門檻、排除 ETF、movers top=8。
- 樣本進度顯示 0 的雙重 bug 已修（commit `71d9a12`）。
- `build_if_stale`：訓練集過期（max_age_days）才重建。
- 純讀本機 parquet、CPU、零外部 API/LLM。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 訓練集 MUST 在歷史回放線上選股規則，與線上同分布（流動性門檻/排除 ETF/top=8）。
- **FR-002**: MUST 防未來洩漏：特徵/排行只用 `≤ D`、標籤只用 `> D`；訓練端 walk-forward 切分。
- **FR-003**: MUST 訓練 per-horizon 方向模型（HistGradientBoosting）並落地；樣本不足 MUST 自動跳過。
- **FR-004**: MUST 以 walk-forward 算 OOS 指標（含 precision@k、機率校準器）。
- **FR-005**: `_apply_edge_scores`/`_apply_rank_scores` MUST guarded：無模型/未過 gate 不重排；例外只記 warning。
- **FR-006**: 重疊樣本 MUST 降權（`_overlap_weights`）。
- **FR-007**: 全流程 MUST 零 LLM/外部 API（純本地 CPU）；`build_if_stale` 控重建頻率。

### Key Entities

- **training set**: `build_training_set` 產出的樣本表（point-in-time 特徵 + 5/20 日標籤 + 權重）。
- **edge/rank model**: per-horizon HistGradientBoosting（`_model_path`/`_rank_model_path`）+ 校準器。
- **OOS 指標**: rank-IC/precision@k 等；rank-IC gate 決定是否重排。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 訓練集 0 未來洩漏；與線上選股同分布。
- **SC-002**: 有足夠樣本時能訓出 per-horizon 模型並產 OOS 指標；不足時安全跳過。
- **SC-003**: serving 打分/重排 guarded，晨報產出不受影響。
- **SC-004**: 純本地、零 LLM 成本。

## Assumptions

- 行情 parquet 由 [001]/[002] 提供；歷史深度由慢爬（[010]）拉長。
- scorecard/live 樣本來自 [012]；meta/風險層見 [014]。
- horizons 預設 5/20 日。
