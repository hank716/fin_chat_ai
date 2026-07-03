# Feature Specification: 策略成效量測 — 晨報回測 + 校準回灌迴圈

**Feature Branch**: `012-strategy-eval-backtest`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: 憲章 IV（point-in-time）、II（零 LLM 成本）；實作 `backend/reports/backtest.py`、
`backend/reports/strategy_calibration.py`（文字校準與成效評估部分）；相依 [006-morning-brief]（注入/迴圈）；
相關 commit `6b6b851`（成效量測 harness）、`81dc142`（回測 + 自動修正迴圈）。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。本 feature 是「策略自動修正
> 迴圈」的**量測 + 文字校準**兩環；ML edge/meta 模型見 [013]/[014]。純本地、零 LLM 成本。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 對過去晨報預估做事後評分（無未來洩漏） (Priority: P1)

對每份過去晨報的候選（tw_watchlist/tw_caution，帶 target/stop/signals），用已落地 parquet 行情回測：
是否觸目標/止損、方向對不對、未來報酬、MFE/MAE、相對大盤超額；聚合成 scorecard 落地。

**Why this priority**: 「準不準」變成數字，是自動修正的前提（commit `6b6b851`）。

**Independent Test**: `evaluate_report(report)` 對 5/20 日雙窗評分；未來窗嚴格 `trade_date > as_of`（杜絕洩漏）。

**Acceptance Scenarios**:

1. **Given** 一份到期晨報，**When** `evaluate_report`，**Then** 每檔各窗得觸價/方向/報酬/MFE/MAE/超額並聚合。
2. **Given** 進場價，**When** 取未來窗，**Then** 僅用 `trade_date > as_of` 的 OHLC（point-in-time）。
3. **Given** 尚未到期的窗，**When** `run_due_evaluations`，**Then** 冪等只評已到期者。

---

### User Story 2 - 把成績濃縮成校準文字回灌晨報 prompt (Priority: P1)

把命中率/止損率/目標價樂觀度/訊號效力濃縮成一段繁中「校準提示」，由 [006] 注入晨報 prompt，
讓 Gemini 下次自我修正選股傾向（第 1 天就有效）。

**Why this priority**: 零成本、即時生效的自我修正（commit `81dc142`）。

**Independent Test**: `build_calibration_block()` 回一段校準文字（scorecard 不足時回空字串）。

**Acceptance Scenarios**:

1. **Given** 有足夠 scorecard，**When** `build_calibration_block`，**Then** 回含命中率/止損率/訊號效力的校準文字。
2. **Given** scorecard 不足，**When** 建構，**Then** 回空字串（不注入）。

---

### User Story 3 - 策略成效評估（是否顯著、可信） (Priority: P2)

`evaluate_effectiveness` 以超額報酬統計 + 樣本量/跨度判斷結論是否充分（verdict），供首頁面板顯示。

**Why this priority**: 避免用少量樣本過度解讀（可信度把關）。

**Independent Test**: `evaluate_effectiveness()` 回超額統計 + verdict（sufficient/樣本數/跨度）。

**Acceptance Scenarios**:

1. **Given** scorecard，**When** `evaluate_effectiveness`，**Then** 回偏多/偏空超額統計與 `_eval_verdict`。

---

### Edge Cases

- 進場價 = 晨報資料日收盤（缺值回退 parquet）；`_data_matured` 判窗是否到期。
- `featurize()` 把個股當日特徵抽成數值向量存進 scorecard，供 [013]/[014] 訓練直接讀（免再載 report.json）。
- 全程讀本機 scorecard/parquet、CPU 數秒、零 LLM/外部 API。
- scorecard 冪等落地 `storage/backtests/{report_id}.json`；`_fully_matured` 後不重評。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: MUST 對過去晨報候選回測，未來窗 MUST 嚴格 `trade_date > as_of`（無未來洩漏，憲章 IV）。
- **FR-002**: MUST 對每檔算觸目標/止損、方向、未來報酬、MFE/MAE、相對大盤超額，聚合成 scorecard。
- **FR-003**: `run_due_evaluations` MUST 冪等、只評已到期窗；scorecard MUST 落地且 `_fully_matured` 後不重評。
- **FR-004**: `featurize()` MUST 把 point-in-time 特徵向量連同標籤存進 scorecard（供 [013]/[014]）。
- **FR-005**: `build_calibration_block` MUST 把成績濃縮成繁中校準文字；不足時回空字串。
- **FR-006**: `evaluate_effectiveness` MUST 回超額統計 + 充分性 verdict（樣本量/跨度）。
- **FR-007**: 全流程 MUST 零 LLM/外部 API（純本地 CPU）。

### Key Entities

- **scorecard**: `storage/backtests/{report_id}.json`（每檔每窗成績 + featurize 向量 + 標籤）。
- **calibration block**: 注入晨報 prompt 的校準文字。
- **effectiveness/verdict**: 超額統計 + 是否充分。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 回測 0 未來洩漏（未來窗嚴格 > as_of）。
- **SC-002**: 校準文字每日可回灌晨報，零額外成本。
- **SC-003**: 成效結論帶充分性判斷，避免小樣本過度解讀。
- **SC-004**: scorecard 冪等、可重跑不重複評分。

## Assumptions

- 行情 parquet 由 [001]/[002] 提供；候選來自 [006] 落地晨報。
- horizons 預設 5/20 日雙窗。
- 硬體現實：本機 GTX 1060 不適合跑 LLM，策略大腦用表格式 ML/統計（見 [013]/[014]）。
