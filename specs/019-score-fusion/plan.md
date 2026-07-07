# Implementation Plan: serving 分數融合優先序 + 小型股 sleeve

**Branch**: `019-score-fusion` | **Date**: 2026-07-07 | **Spec**: [spec.md](./spec.md)

**Note**: 正向新規格（`OPTIMIZATION_PLAN.md` WP2.2+WP2.3）。架構決策已定案；US1/US2 程式+測試完成。

## Summary

把 serving 端方向分數（edge/rank/qlib）從 last-writer-wins 逐一 sort，改為 `fuse_scores` 單一融合入口
（過 gate 者 z-score 加權平均、只重排一次、歸因明確）；並把 rank 依候選 amount 分流至小型股帶模型
（baseline 證實 alpha 在此）。純本地、零 LLM；fail-closed（未過 gate 不動）。

## Technical Context

**Language/Version**: Python 3.12（backend 容器）
**Primary Dependencies**: numpy/pandas/scikit-learn/joblib；training_set band（[013]）
**Storage**: `strategy/rank_model_smallcap_{h}.pkl`、`rank_model_meta_smallcap.json`、`training_set_smallcap.parquet`
**Testing**: pytest（`tests/test_score_fusion.py`，含 fusion + band routing）
**Constraints**: fail-closed gate；report 向後相容個別分數欄；融合一次；零 LLM
**Scale/Scope**: `strategy_calibration.py`（fuse_scores/_meta_margin/band rank）、`morning_brief.py`（融合入口 + 管線 hook）

## Constitution Check

- **III. Fail-Closed** — ✅ 未過 gate 不重排；band 模型不存在/未過 gate 該帶回空。
- **IV. Point-in-Time** — ✅ 沿用 purged walk-forward + embargo；band 訓練集同紀律。
- **II. 成本紀律** — ✅ 純本地 CPU、零 LLM；小型股帶訓練集過舊才重建。
- **VI. 服務隔離** — ✅ qlib 仍只讀 JSON、永不 import qlib（fuse_scores 讀 score_qlib 的既有結果）。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本規格先行；commit 引用 `specs/019`。

**設計取捨（非違規）**：融合權重＝『OOS 指標超出 gate 幅度』（簡單、可解讀、與 gate 一致）；rank 融合
權重取主池/小型股帶較強者。**結論**：通過。

## Project Structure

### Source Code (repository root)

```text
backend/reports/strategy_calibration.py   # fuse_scores / _meta_margin / _rank_meta_path /            [US1+US2]
                                          #   train_rank_model(band) / _ensure_smallcap_training_set /
                                          #   _score_rank_band / score_rank(依 _amount 分流)
backend/reports/morning_brief.py           # 方向融合單一入口（取代 _apply_edge/rank/qlib）+           [US1+US2]
                                          #   report fused_scores/fusion_weights + 管線 train band
backend/reports/training_set.py            # build_training_set(min/max_amount, out_path) 已支援        [既有]
tests/test_score_fusion.py                 # fusion 三情境 + band routing + 路徑                        [US1+US2]
```

**Structure Decision**: 融合邏輯集中在 strategy_calibration（可單元測）；morning_brief 只做「呼叫融合 →
依 fused 重排一次」。band 分流在 score_rank 內部，對呼叫端透明。

## Complexity Tracking

> 無 Constitution 違規，免填。
