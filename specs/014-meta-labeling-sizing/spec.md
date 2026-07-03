# Feature Specification: Meta-labeling + 部位 sizing + 市場曝險

**Feature Branch**: `014-meta-labeling-sizing`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: 憲章 IV（point-in-time）、II（零 LLM）、III（風險側 fail-closed 精神）；
實作 `backend/reports/training_set.py`（triple-barrier 標籤）、`backend/reports/strategy_calibration.py`
（meta/risk 模型）、`backend/reports/morning_brief.py`（`_apply_meta_scores`/`_apply_risk_scores`/
`_apply_sizing`）、`backend/processor/market_regime.py`（曝險係數）；相依 [013]、[016]；
相關 commit `2ecb82a`（meta-labeling）、`892c129`（sizing + 淨 P&L）。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。這是「風險側→會不會賺錢」的
> 一環：meta 標把握度、risk 標回撤、sizing 合成部位權重、市場恐慌調總曝險。純本地、零 LLM。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Triple-barrier 標籤 + meta-labeling 把握度 (Priority: P1)

以 triple-barrier（上/下障礙 + 時間障礙）產生標籤；訓練 meta 模型標「訊號成功機率（conviction）」，
供 sizing/過濾，不改變方向。

**Why this priority**: 方向對不代表會賺；meta 把「該不該重押」量化（commit `2ecb82a`）。

**Independent Test**: `_triple_barrier(close,high,low,...)` 產標籤；`train_meta_model` 產 conviction 模型。

**Acceptance Scenarios**:

1. **Given** OHLC 序列，**When** `_triple_barrier`，**Then** 依先觸上/下/時間障礙給標籤。
2. **Given** meta 樣本，**When** 訓練，**Then** 產把握度模型；`_apply_meta_scores` 標 conviction（不重排方向）。

---

### User Story 2 - 回撤風險模型（風險側 fail-closed 精神） (Priority: P1)

risk 模型以未來 h 日 MAE 中位數切分訓練；`_apply_risk_scores` 標記偏多高風險並強化避雷側排序。

**Why this priority**: 錯的偏多訊號傷害大；風險側優先「避雷」（憲章 III 精神）。

**Independent Test**: `train_risk_model` 產風險模型；`_apply_risk_scores` 對偏多候選標高風險。

**Acceptance Scenarios**:

1. **Given** 有風險模型且過 gate，**When** `_apply_risk_scores`，**Then** 標記高風險 + 強化避雷排序。

---

### User Story 3 - 部位 sizing × 市場曝險覆蓋 (Priority: P2)

`_apply_sizing`：risk×meta 合成偏多書權重，再乘市場恐慌曝險係數（[016] market_fear）產出部位權重；
淨 P&L 回測把風險側分數翻成「會不會賺錢」的證據。

**Why this priority**: 把分數變成可執行的部位大小與可驗證的損益（commit `892c129`）。

**Independent Test**: `_apply_sizing(result, feats)` 回 `size_weights` + `market_fear`；兩道 gate 未過則不動。

**Acceptance Scenarios**:

1. **Given** 有 meta/risk 分數且過 gate，**When** `_apply_sizing`，**Then** 產部位權重 × 市場曝險係數。
2. **Given** 任一 gate 未過，**When** `_apply_sizing`，**Then** 不動（guarded no-op）。

---

### Edge Cases

- 所有打分 serving 為 guarded：未過 gate 或無模型不動；例外只記 warning（[006]）。
- meta 只標把握度，不改方向；risk 偏「避雷」而非追多。
- 市場級序列（P/C ratio）對橫斷面 risk/meta 近乎無效，主要供市場 regime/總曝險（見 [016]）。
- 純本地 CPU、零 LLM/外部 API；標籤/訓練防未來洩漏（同 [013] 三律）。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: MUST 以 triple-barrier 產生標籤（上/下/時間障礙），供 meta/risk 訓練。
- **FR-002**: meta 模型 MUST 標把握度（conviction）供 sizing/過濾，MUST NOT 改變方向。
- **FR-003**: risk 模型 MUST 以未來 h 日 MAE 切分訓練；`_apply_risk_scores` MUST 標高風險並強化避雷排序。
- **FR-004**: `_apply_sizing` MUST 由 risk×meta 合成權重 × 市場恐慌曝險係數（[016]）產部位權重。
- **FR-005**: 淨 P&L 回測 MUST 把風險側分數對應到損益證據。
- **FR-006**: 所有 serving 打分 MUST guarded：未過 gate/無模型不動、例外只記 warning。
- **FR-007**: 標籤/訓練 MUST 防未來洩漏；全流程 MUST 零 LLM/外部 API。

### Key Entities

- **triple-barrier 標籤 / meta 樣本**: `_triple_barrier`、`_live_meta_samples`。
- **meta/risk model**: `_meta_model_path`/`_risk_model_path`（per-horizon）。
- **size_weights / market_fear**: 部位權重 + 市場曝險係數（[016]）。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: meta 標把握度但不改方向；risk 正確標高風險偏多候選。
- **SC-002**: sizing 產出合理部位權重並隨市場恐慌調整總曝險。
- **SC-003**: 淨 P&L 回測能把風險側分數對應到損益（可驗證有效性）。
- **SC-004**: serving 全 guarded，晨報產出不受影響；零 LLM 成本。

## Assumptions

- 特徵/標籤與 [013] 共用 point-in-time 管線；市場曝險來自 [016]。
- 打分在 [006] 以 guarded 呼叫、兩道 gate 守門。
- horizons 預設 5/20 日；模型為表格式 ML（本機 CPU）。
