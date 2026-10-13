"""選股歸因的彙總數學（純函式；不碰 parquet）。"""
from __future__ import annotations

import pandas as pd
import pytest

from reports import selection_attribution as sa


def _day(as_of, wl, pool, *, risk_ic=None):
    day = {"as_of": as_of, "scores": {}, "groups": {
        "llm_watchlist": {"n": 5, "excess": wl, "mae": -3.0},
        "pool_bull": {"n": 16, "excess": pool, "mae": -4.0},
    }}
    if risk_ic is not None:
        day["scores"]["risk_scores"] = {"n": 10, "ic_excess": 0.0, "ic_drawdown": risk_ic}
    return day


def test_summary_is_day_weighted_with_t_stat():
    s = sa._summary([1.0, 3.0, -1.0, 1.0])
    assert s["n_days"] == 4 and s["mean"] == pytest.approx(1.0)
    assert s["positive_rate"] == 0.75
    assert s["t"] == pytest.approx(1.0 / (((0 + 4 + 4 + 0) / 3) ** 0.5 / 2), abs=0.01)
    assert sa._summary([]) == {"n_days": 0}


def test_picks_are_compared_with_same_day_pool():
    """LLM 實選要跟**同一天**的候選池比，不是跟全期平均比——否則行情好壞會混進挑選能力。"""
    agg = sa.aggregate([_day("2026-09-01", 2.0, 1.0), _day("2026-09-02", -3.0, -1.0)], 5)
    assert agg["llm_watchlist_minus_pool_bull"]["mean"] == pytest.approx(-0.5)
    assert agg["groups"]["llm_watchlist"]["excess_pct"]["mean"] == pytest.approx(-0.5)
    assert agg["date_range"] == ["2026-09-01", "2026-09-02"]


def test_score_ic_skips_days_without_that_score():
    agg = sa.aggregate([_day("2026-09-01", 0, 0, risk_ic=0.4), _day("2026-09-02", 0, 0)], 5)
    assert agg["score_ic"]["risk_scores"]["ic_drawdown"]["n_days"] == 1


def test_outcome_uses_only_bars_after_as_of(monkeypatch):
    fwd = pd.DataFrame({"close": [101.0, 110.0], "low": [95.0, 99.0]})
    monkeypatch.setattr(sa.backtest, "_entry_close_adj", lambda sym, as_of: 100.0)
    monkeypatch.setattr(sa.backtest, "_forward_window", lambda sym, as_of, h, adjusted=False: fwd.head(h))
    out = sa._outcome("2330", pd.Timestamp("2026-09-01"), 2, index_ret=4.0)
    assert out == {"ret": pytest.approx(10.0), "excess": pytest.approx(6.0), "mae": pytest.approx(-5.0)}
    assert sa._outcome("2330", pd.Timestamp("2026-09-01"), 3, index_ret=4.0) is None   # 窗未滿不估
