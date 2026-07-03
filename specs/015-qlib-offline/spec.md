# Feature Specification: Qlib 離線整合（隔離 image + gate 守護 serving）

**Feature Branch**: `015-qlib-offline`

**Created**: 2026-07-03

**Status**: Baseline（回溯補規格 — 描述已實作之現況行為）

**來源交叉引用**: 憲章 VI（服務隔離）、IV（point-in-time）、II（零 LLM）；實作 `qlib_offline/`
（`run.py`、`dump.py`、`common.py`、`Dockerfile`）、serving 端 `backend/reports/morning_brief.py`
（`_apply_qlib_scores`）；相依 [013]（rank 定義對齊）；相關 commit `271bf6f`。

> 本規格以「現況行為」反寫，作為基線。與 `design_docs.md` 衝突時以本檔為準。Qlib 訓練在**隔離離線 image**
> 執行，serving（backend）只讀 JSON、**永不 import qlib**，由 gate 守護。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 隔離離線評估（Alpha158 能否破方向牆） (Priority: P1)

離線 image 用 Qlib Alpha158（158 因子）+ LightGBM 做 purged walk-forward，評估方向 rank-IC/ICIR
與風險 OOS AUC，落地 `storage/strategy/qlib_eval.json`。

**Why this priority**: 核心問題——Alpha158 能否突破 0.027 方向牆（commit `271bf6f`）。

**Independent Test**: `run.py --eval` 產 `qlib_eval.json`；rank 定義對齊 backend `_evaluate_rank_oos`。

**Acceptance Scenarios**:

1. **Given** 本機 parquet dump 成 Qlib 格式，**When** `--eval`，**Then** 產逐日橫斷面 rank-IC/ICIR + 風險 AUC。
2. **Given** 評估完成，**When** 落地，**Then** 寫 `qlib_eval.json`（serving 可讀）。

---

### User Story 2 - 訓練 + 打分 + gate meta (Priority: P1)

`--score` 訓練後替最近交易日流動性股池打分，落地 `qlib_scores/{date}.json` + `qlib_meta.json`（含 gate 指標）；
serving 端 `_apply_qlib_scores` 過 rank-IC gate 才重排偏多。

**Why this priority**: 只有通過 gate 的離線分數才允許影響晨報（品質守門）。

**Independent Test**: `run.py --score` 產分數 + meta；`_apply_qlib_scores` 無檔或未過 gate → 不動。

**Acceptance Scenarios**:

1. **Given** 訓練完成，**When** `--score`，**Then** 產 `qlib_scores/{date}.json` + `qlib_meta.json`。
2. **Given** 未過 rank-IC gate 或無離線檔，**When** `_apply_qlib_scores`，**Then** 不重排（guarded）。

---

### User Story 3 - 容器自帶排程、與 serving 完全隔離 (Priority: P2)

`--loop` 啟動跑一次 eval+score，之後每日 off-peak（03:00 台北）重跑（容器自帶排程，不需 scheduler）；
serving 永不 import qlib、只讀 JSON。

**Why this priority**: 隔離避免 qlib 重相依污染 serving image（憲章 VI）。

**Independent Test**: serving 程式無任何 `import qlib`；離線 image 由 compose 獨立跑。

**Acceptance Scenarios**:

1. **Given** `--loop`，**When** 啟動，**Then** 立即 eval+score，之後每日 03:00 重跑。
2. **Given** serving 讀分數，**When** 匯入檢查，**Then** backend 無 qlib 相依。

---

### Edge Cases

- 純離線、讀本機 parquet、零外部 API、零 LLM。
- purged walk-forward 防未來洩漏；rank 定義對齊 [013] `_evaluate_rank_oos`。
- gate（rank-IC）未過 → serving 不採用該分數（fail-closed 品質守門）。
- 離線 image 相依（qlib/lightgbm）不得進入 serving image（隔離）。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 離線 image MUST 用 Qlib Alpha158 + LightGBM 做 purged walk-forward；`--eval` 落地 `qlib_eval.json`。
- **FR-002**: rank 指標定義 MUST 對齊 backend `_evaluate_rank_oos`（[013]）；風險用未來 h 日 MAE OOS AUC。
- **FR-003**: `--score` MUST 訓練 + 打分最近交易日流動性股池，落地 `qlib_scores/{date}.json` + `qlib_meta.json`。
- **FR-004**: serving `_apply_qlib_scores` MUST 過 rank-IC gate 才重排；無檔/未過 gate MUST 不動（guarded）。
- **FR-005**: serving（backend）MUST NOT import qlib；只讀 JSON。
- **FR-006**: `--loop` MUST 容器自帶排程（每日 off-peak 03:00），不依賴 [010] scheduler。
- **FR-007**: 全流程 MUST 零 LLM/外部 API；防未來洩漏（purged walk-forward）。

### Key Entities

- **qlib_eval.json**: 方向 rank-IC/ICIR + 風險 AUC 評估。
- **qlib_scores/{date}.json + qlib_meta.json**: 打分 + gate 指標。
- **isolated image**: `qlib_offline/Dockerfile`（qlib/lightgbm，獨立於 serving）。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 離線可產出方向/風險 OOS 評估，回答「Alpha158 能否破方向牆」。
- **SC-002**: 只有過 gate 的離線分數影響晨報；未過則零影響。
- **SC-003**: serving image 無 qlib 相依（隔離成立）。
- **SC-004**: 純離線、零 LLM/外部 API；每日 off-peak 自動重跑。

## Assumptions

- 本機 parquet 由 [001]/[002] 提供，`dump.py` 轉 Qlib 格式。
- serving 與離線以檔案（JSON）為介面契約；compose 各自服務。
- horizons/rank 定義與 [013] 對齊以可比較。
