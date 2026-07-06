"""防未來洩漏測試：strategy_calibration._evaluate_oos 的 purged walk-forward + embargo。

覆蓋 WP0.1 的第 6 組。用 monkeypatch 攔截 _fit_predict 記錄每個 fold 實際收到的
train / test 列，直接驗證「embargo 內（test 起始日前 h 個交易日）的樣本不得進訓練折」。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from reports import strategy_calibration as sc


def _make_ds(n_dates: int = 60, per_day: int = 12) -> pd.DataFrame:
    """造 n_dates 個相異日期、每日 per_day 筆、兩類齊全的資料集（__y/__d/__w + 特徵欄）。"""
    dates = [f"2024-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n_dates)]
    rows = []
    for d in dates:
        for j in range(per_day):
            row = {c: 0.0 for c in sc.FEATURE_COLUMNS}
            row["__y"] = j % 2                          # 每日兩類齊全
            row["__d"] = d
            row["__w"] = 1.0
            rows.append(row)
    return pd.DataFrame(rows)


def test_purged_walk_forward_enforces_embargo(monkeypatch):
    """每個 fold 的最大訓練日位置 < 測試起始日位置 − h（embargo 至少隔 h 個交易日）。

    打破會 fail：若把 embargo `dpos < (ts - h)` 改成 `dpos < ts`（無 embargo），
    最大訓練日位置會貼到 min_test-1，assert `max_train <= min_test - h - 1` 立即紅掉。
    """
    h = 5
    ds = _make_ds()
    date_pos = {d: i for i, d in enumerate(sorted(ds["__d"].unique()))}
    row_dpos = ds["__d"].map(date_pos)

    captured: list[tuple[list, list]] = []

    def fake_fit_predict(Xtr, ytr, Xte, wtr=None, feature_cols=sc.FEATURE_COLUMNS):
        captured.append((list(Xtr.index), list(Xte.index)))
        return object(), list(feature_cols), np.full(len(Xte), 0.5)

    monkeypatch.setattr(sc, "_fit_predict", fake_fit_predict)
    res = sc._evaluate_oos(ds, h, with_importance=False)

    assert res["eval_method"] == "purged_walk_forward"
    assert res["cv_folds"] >= 1
    assert captured, "應至少跑一個 fold"
    for tr_idx, te_idx in captured:
        max_train = max(row_dpos[i] for i in tr_idx)
        min_test = min(row_dpos[i] for i in te_idx)
        assert max_train <= min_test - h - 1
