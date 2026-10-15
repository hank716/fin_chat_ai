"""spec 012 測試基線：單檔回測計算、到期評估冪等、校準文字門檻、成效判讀隨樣本變化。

這些數字是策略校準與所有「準不準」結論的依據——算錯一個符號，整套評估就跟著錯。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from reports import backtest as bt
from reports import strategy_calibration as sc


def _ohlc(rows):
    return pd.DataFrame(rows, columns=["high", "low", "close"])


# ── T011 觸目標/止損/方向/MFE/MAE/超額 ──

def test_bull_target_hit_on_day_two_with_metrics():
    fwd = _ohlc([(103, 99, 102), (111, 101, 108), (109, 104, 105)])
    r = bt.evaluate_item("watchlist", 100.0, 110.0, 95.0, fwd, index_ret=2.0)
    assert (r["outcome"], r["days_to_event"], r["n_days"]) == ("target_hit", 2, 3)
    assert r["forward_return_pct"] == 5.0
    assert (r["mfe_pct"], r["mae_pct"]) == (11.0, -1.0)
    assert r["implied_target_pct"] == 10.0
    assert r["direction_correct"] is True and r["vs_index_pct"] == 3.0


def test_same_day_target_and_stop_counts_as_stop():
    fwd = _ohlc([(112, 94, 100)])
    assert bt.evaluate_item("watchlist", 100.0, 110.0, 95.0, fwd, None)["outcome"] == "stop_hit"


def test_caution_side_is_mirrored():
    fwd = _ohlc([(101, 92, 93)])                    # 跌 7%：偏空方向對，觸「下方目標」90? 未到
    r = bt.evaluate_item("caution", 100.0, 90.0, 105.0, fwd, index_ret=None)
    assert r["outcome"] == "open" and r["direction_correct"] is True
    assert r["vs_index_pct"] is None
    up = bt.evaluate_item("caution", 100.0, 90.0, 105.0, _ohlc([(106, 99, 104)]), None)
    assert up["outcome"] == "stop_hit" and up["direction_correct"] is False


def test_returns_use_adjusted_prices_but_triggers_use_nominal():
    """除息日：名目價跌、還原價連續——報酬看還原，觸價看名目（spec 017 雙軌）。"""
    fwd = _ohlc([(97, 94, 95)])                     # 名目：除息 5 元後 95
    fwd_adj = _ohlc([(102, 99, 100)])               # 還原：其實沒跌
    r = bt.evaluate_item("watchlist", 100.0, None, 95.0, fwd, None, entry_adj=100.0, fwd_adj=fwd_adj)
    assert r["forward_return_pct"] == 0.0           # 還原價報酬
    assert r["outcome"] == "stop_hit"               # 名目價觸止損


def test_adjusted_window_length_mismatch_falls_back_to_nominal():
    fwd = _ohlc([(101, 99, 100), (103, 100, 102)])
    r = bt.evaluate_item("watchlist", 100.0, None, None, fwd, None,
                         entry_adj=50.0, fwd_adj=_ohlc([(51, 49, 50)]))
    assert r["forward_return_pct"] == 2.0


@pytest.mark.parametrize("fwd,entry", [(_ohlc([]), 100.0), (_ohlc([(1, 1, 1)]), 0.0)])
def test_no_data_when_window_empty_or_no_entry(fwd, entry):
    assert bt.evaluate_item("watchlist", entry, None, None, fwd, None)["outcome"] == "no_data"


@pytest.mark.parametrize("raw,expected", [("480.0", 480.0), ("約 480", 480.0), ("480~500", 480.0),
                                          (512, 512.0), (None, None), ("無", None), ("0", None)])
def test_parse_price_tolerates_free_text(raw, expected):
    assert bt._parse_price(raw) == expected


# ── T012 run_due_evaluations：冪等、只評到期、壞檔重算、單份失敗不阻斷 ──

@pytest.fixture
def dirs(tmp_path, monkeypatch):
    rep, out = tmp_path / "reports", tmp_path / "backtests"
    rep.mkdir()
    monkeypatch.setattr(bt, "REPORTS_DIR", rep)
    monkeypatch.setattr(bt, "BACKTEST_DIR", out)
    return rep, out


def _write_report(rep, rid):
    (rep / f"{rid}.json").write_text(json.dumps({"report_id": rid}), encoding="utf-8")


def test_due_evaluations_skip_fully_matured_and_recompute_the_rest(dirs, monkeypatch):
    rep, out = dirs
    for rid in ("morning_20261001_082000", "morning_20261008_082000", "morning_20261014_082000"):
        _write_report(rep, rid)
    out.mkdir()
    (out / "morning_20261001_082000.json").write_text(json.dumps({"matured": {"5": True, "20": True}}))
    (out / "morning_20261008_082000.json").write_text(json.dumps({"matured": {"5": True, "20": False}}))
    seen = []

    def fake_eval(report, hs):
        seen.append(report["report_id"])
        return {"report_id": report["report_id"], "matured": {str(h): True for h in hs}}

    monkeypatch.setattr(bt, "evaluate_report", fake_eval)
    first = bt.run_due_evaluations([5, 20])
    assert seen == ["morning_20261008_082000", "morning_20261014_082000"]
    assert (first["evaluated"], first["skipped"]) == (2, 1)
    second = bt.run_due_evaluations([5, 20])              # 全部到期後重跑＝全跳過
    assert (second["evaluated"], second["skipped"]) == (0, 3)


def test_corrupt_scorecard_is_recomputed_and_one_failure_does_not_block(dirs, monkeypatch):
    rep, out = dirs
    _write_report(rep, "morning_20261001_082000")
    _write_report(rep, "morning_20261002_082000")
    out.mkdir()
    (out / "morning_20261001_082000.json").write_text("{not json")

    def fake_eval(report, hs):
        if report["report_id"].endswith("02_082000"):
            raise ValueError("bad report")
        return {"matured": {"5": True}}

    monkeypatch.setattr(bt, "evaluate_report", fake_eval)
    res = bt.run_due_evaluations([5])
    assert (res["evaluated"], res["errors"]) == (1, 1)
    assert json.loads((out / "morning_20261001_082000.json").read_text()) == {"matured": {"5": True}}


def test_due_evaluations_without_reports_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "REPORTS_DIR", tmp_path / "nope")
    assert bt.run_due_evaluations([5])["evaluated"] == 0


# ── T013 校準文字：樣本足夠才有、不足/停用回空 ──

def _summary(n, dir_acc=0.6, optimism=1.5):
    return {"sample_n": n, "primary_horizon": 5, "signal_ranking": [], "edge_model": {},
            "metrics": {"watchlist": {"5": {"n": n, "direction_accuracy": dir_acc,
                                             "target_hit_rate": 0.2, "stop_hit_rate": 0.5,
                                             "avg_forward_return_pct": 1.2,
                                             "target_optimism": optimism}}}}


def test_compose_text_empty_below_min_samples():
    assert sc._compose_text(_summary(sc.MIN_TEXT_SAMPLES - 1)) == ""


def test_compose_text_with_enough_samples_gives_actionable_lines():
    text = sc._compose_text(_summary(sc.MIN_TEXT_SAMPLES))
    assert text.startswith("【策略校準")
    assert "方向準確率 60%" in text
    assert "目標價偏樂觀" in text and "止損觸發偏頻繁" in text


def test_build_calibration_block_reads_file_and_respects_switch(tmp_path, monkeypatch):
    path = tmp_path / "calibration.json"
    monkeypatch.setattr(sc, "CALIBRATION_PATH", path)
    monkeypatch.setattr(sc.settings, "enable_strategy_calibration", True)
    assert sc.build_calibration_block() == ""             # 無檔
    path.write_text(json.dumps({"calibration_text": "校準內容"}), encoding="utf-8")
    assert sc.build_calibration_block() == "校準內容"
    monkeypatch.setattr(sc.settings, "enable_strategy_calibration", False)
    assert sc.build_calibration_block() == ""


# ── T014 成效判讀：樣本量與交易日數決定能不能下定論 ──

def _scorecards(n_dates, per_day, excess):
    out = []
    for d in range(n_dates):
        out.append({"as_of": f"2026-09-{d + 1:02d}", "items": [
            {"side": "watchlist", "horizons": {"5": {"outcome": "open", "forward_return_pct": 1.0,
                                                    "vs_index_pct": excess, "direction_correct": True}}}
            for _ in range(per_day)]})
    return out


@pytest.fixture
def eval_env(tmp_path, monkeypatch):
    for name in ("EDGE_META_PATH", "RISK_META_PATH", "RANK_META_PATH", "QLIB_META_PATH",
                 "META_META_PATH"):
        monkeypatch.setattr(sc, name, tmp_path / f"{name}.json")
    monkeypatch.setattr(sc, "STRATEGY_DIR", tmp_path)
    monkeypatch.setattr(sc, "EVAL_PATH", tmp_path / "evaluation.json")
    monkeypatch.setattr(sc.backtest, "horizons", lambda: [5, 20])
    monkeypatch.setattr(sc.settings, "edge_model_min_samples", 50)
    from reports import smallcap_sleeve
    monkeypatch.setattr(smallcap_sleeve, "live_stats", lambda scs: {"n": 0})

    def use(scs):
        monkeypatch.setattr(sc.backtest, "load_scorecards", lambda limit=None: scs)

    return use


def test_few_samples_verdict_says_not_enough(eval_env):
    eval_env(_scorecards(n_dates=3, per_day=5, excess=2.0))
    out = sc.evaluate_effectiveness()
    assert out["data"]["sufficient"] is False
    assert out["verdict"].startswith("⚠️ 樣本仍不足")
    assert out["watchlist"]["avg_excess_vs_market_pct"] == 2.0


def test_enough_samples_across_dates_verdict_is_conclusive(eval_env):
    eval_env(_scorecards(n_dates=sc.EVAL_MIN_DATES, per_day=6, excess=-1.5))
    out = sc.evaluate_effectiveness()
    assert out["data"]["sufficient"] is True
    assert out["data"]["date_span_days"] == sc.EVAL_MIN_DATES - 1
    assert out["verdict"].startswith("資料量已具參考性")
    assert "輸大盤 1.5%" in out["verdict"] and "未加值" in out["verdict"]


def test_many_samples_on_few_dates_is_still_insufficient(eval_env):
    eval_env(_scorecards(n_dates=2, per_day=100, excess=1.0))   # 量多但只涵蓋兩天行情
    assert sc.evaluate_effectiveness()["data"]["sufficient"] is False
