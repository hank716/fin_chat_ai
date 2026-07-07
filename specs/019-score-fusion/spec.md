# Feature Specification: serving 分數融合優先序 + 小型股 sleeve

**Feature Branch**: `019-score-fusion`

**Created**: 2026-07-07

**Status**: In progress（US1/WP2.2 + US2/WP2.3 程式+測試完成，pending live 驗證）

**來源交叉引用**: `OPTIMIZATION_PLAN.md` WP2.2+WP2.3、全域決策 D1（歸因）；憲章 III（fail-closed gate）、
IV（point-in-time）、II（零 LLM）。依賴 [013] training_set（band 已支援 min/max_amount）、
[012] backtest gate、[014] sizing。baseline/adj 對照見 `eval_history/*_adj_prices.json`。

> **問題**：serving 端 `_apply_edge/rank/qlib` 逐一 sort watchlist＝last-writer-wins，歸因不明（多模型過
> gate 時最後者靜默覆蓋）；且方向模型撞效率牆，但 baseline 實測**小型股 h5 rank-IC ~0.076 遠強於主池
> ~0.034**——火力應往有 edge 處重分配。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 方向分數融合（WP2.2） (Priority: P1) ✅

`fuse_scores(candidates)`：把**通過各自 gate** 的方向模型(edge/rank/qlib)分數 z-score 標準化後，以
『OOS 指標超出 gate 的幅度』為權重加權平均 → 單一 fused 排序分數，watchlist 只重排一次。report JSON
保留個別 `edge_scores/rank_scores/qlib_scores`（向後相容），新增 `fused_scores` 與 `fusion_weights`。

**Why this priority**: 消除 last-writer-wins 的靜默覆蓋、歸因明確；為 WP2.3 分流的融合入口。

**Independent Test**: 單模型過 gate → 排序=該模型；多模型 → 加權；全不過 → 空（不重排）。

**Acceptance Scenarios**:

1. **Given** 僅 rank 過 gate，**When** `fuse_scores`，**Then** 融合排序=rank 排序、權重全歸 rank。
2. **Given** edge+rank 皆過，**When** `fuse_scores`，**Then** 權重∝超 gate 幅度、和=1、z-score 加權。
3. **Given** 全不過 gate，**When** `fuse_scores`，**Then** 回空 → 呼叫端不重排。

---

### User Story 2 - 小型股 sleeve 正式化（WP2.3） (Priority: P1) ✅

`train_rank_model(band="smallcap")` 用小型股帶訓練集（amount∈[5M,50M)）訓另一組 rank 模型 + meta；
`score_rank` 依候選 `_amount` 分流：<50M 走小型股帶模型、其餘走主池，各過各自 gate。掛進每日
`_run_backtest_loop`。`fuse_scores` 的 rank 權重取兩帶較強者。

**Why this priority**: baseline 證實 alpha 在小型股帶（rank-IC ~0.076 vs 主池 ~0.034），分流讓小型股候選
吃到更強排序訊號。

**Independent Test**: amount<50M 候選走 smallcap 帶；band 模型檔/meta 落地；OOS rank-IC 記錄。

**Acceptance Scenarios**:

1. **Given** 候選含 <50M 與 >=50M，**When** `score_rank`，**Then** 分別走 smallcap/主池模型。
2. **Given** 訓練，**When** `train_rank_model(band="smallcap")`，**Then** 落地 band 模型+meta、記 OOS rank-IC。

---

### Edge Cases

- 全帶/全方向模型皆不過 gate → 不重排（fail-closed，維持效率牆保護）。
- 缺 `_amount` 的候選預設走主池（安全預設）。
- 小型股帶訓練集過舊（>3 天）才重建（掃全 parquet，較貴）。
- report JSON 個別分數欄位保留（向後相容）；融合只發生一次。
- 純本地運算、零 LLM。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `fuse_scores` MUST 只納入過各自 gate 的方向模型，z-score 標準化後以超 gate 幅度加權；全不過回空。
- **FR-002**: report JSON MUST 保留個別 `edge/rank/qlib_scores`，新增 `fused_scores`/`fusion_weights`；重排一次。
- **FR-003**: `score_rank` MUST 依候選 `_amount` 分流（<50M→smallcap 帶、其餘→主池），各過各自 gate。
- **FR-004**: `train_rank_model(band)` MUST 產獨立 band 模型檔（`rank_model_{band}_{h}.pkl`）+ meta；掛每日管線。
- **FR-005**: 全流程 MUST fail-closed（未過 gate 不動排序）、point-in-time、零 LLM。

### Key Entities

- **fuse_scores**: `(fused, weights, components)`；weights 和=1。
- **rank_model_smallcap_{h}.pkl** / **rank_model_meta_smallcap.json**: 小型股帶模型與 meta。
- **training_set_smallcap.parquet**: 小型股帶訓練集（[5M,50M)）。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 多模型過 gate 時融合可歸因（fusion_weights 落 report）、無 last-writer-wins（US1）。
- **SC-002**: amount<50M 候選走 smallcap 帶模型（US2）。
- **SC-003**: band 模型+meta 落地、OOS rank-IC 實測記錄（US2）。
- **SC-004**: fail-closed、零 LLM、向後相容個別分數欄位。

## Assumptions

- training_set band 由 min/max_amount + out_path 支援（[013]）；gate 閾值來自 config（rank_ic_gate 等）。
- 小型股帶 alpha 由 baseline `eval_history` 佐證（[012]）。
