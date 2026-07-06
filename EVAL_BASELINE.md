# 評估基線（WP0.2 實跑結果）

> 這是第一次在本 checkout 實跑訓練 pipeline 拿到的**真實** OOS 指標。
> 在此之前所有成績數字都只存在程式註解裡（`storage/strategy/` 從未被填過）。
> 快照 JSON 落在 `storage/strategy/eval_history/`（gitignored），本檔記錄數字與結論以免遺失。

## 重現指令

```bash
# 需求：pandas/numpy/scikit-learn/pyarrow/httpx/redis/tenacity（見 .venv 或 backend/requirements.txt）
LOCAL_STORAGE_PATH="$PWD/storage" PYTHONPATH=backend \
  python -m reports.eval_snapshot --tag baseline --max-date 2026-06-02
# 之後任何準確度改動：同 max_date 再跑一次，再對照
LOCAL_STORAGE_PATH="$PWD/storage" PYTHONPATH=backend \
  python -m reports.eval_snapshot --compare <baseline.json> <new.json>
```

## 資料快照

- **git**：`79a1d87`，`max_date=2026-06-02`
- **訓練集**：3,943 列、76 個交易日、1,352 / 2,363 檔有樣本
- **資料深度只有約 3 個月**（2026-03-04 ～ 2026-05-26），**非** 2 年 —— `history_crawl` 尚未跑深。
  這是本次基線最大的統計力限制：h=20 窗只切得出 2 個 walk-forward fold、meta h20 甚至退回單次切分（cv_folds=0），
  20 日的數字**噪音很大、不可過度解讀**。

## 宣稱值（註解） vs 實測值（baseline）

| 指標 | 註解宣稱 | 實測 h5 | 實測 h20 | 判讀 |
|------|---------|---------|----------|------|
| 方向 edge OOS AUC | ≈0.51–0.53（噪音帶） | **0.495** | 0.511 | ✅ 符合：方向撞效率牆，無 edge |
| 報酬 rank-IC | ≈0.027 | **0.0424** | 0.0168 | ✅ 同量級；h5 略過 gate(0.03)、h20 不過 |
| 回撤風險 OOS AUC | ≈0.69 | **0.618** | 0.509 | ⚠️ 低於宣稱：資料淺、fold 少；h5 仍是四類模型最有 edge 之一 |
| 小型股 rank-IC（5M–50M） | ≈0.06 | **0.0554** | -0.0194 | ✅ 5日符合 ~0.06；20日翻負（樣本淺） |
| meta「該不該下手」AUC | （噪音帶門檻 0.55） | **0.619** | 0.771* | h5 過 gate；h20 單次切分*不可信 |

\* meta h20 `cv_folds=0`（樣本不足退回單次 80/20 切分），0.771 是偽高，勿採信。

小型股 sleeve 扣成本前：選股 alpha 5日 **+0.85%**、top-K 勝全池比率 0.63（46 個 OOS 交易日）。

## 結論

1. **Pipeline 端到端可跑、產出真實 OOS 指標**——評估基線機制（`eval_snapshot`）建立完成，之後每項準確度改動都能同快照 A/B 歸因。
2. **方向牆確認**：edge AUC ≈0.5，符合設計預期（液態股 5–20 日方向接近效率牆）。
3. **有 edge 的方向**：風險模型（h5 AUC 0.618）、meta 下手模型（h5 AUC 0.619 / precision@10% 0.419 vs base 0.272）、小型股 5 日 rank（IC 0.055）——與計畫「火力重分配到風險/小型股」的假設一致。
4. **最大限制是資料深度**：3 個月 → 20 日窗統計力不足。**在做 Phase 1/2 的準確度改動前，優先把 `history_crawl` 跑到 ~2 年**，否則 h20 的 A/B delta 不可歸因。這是後續 WP 的隱含前置。
