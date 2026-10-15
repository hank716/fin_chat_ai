"""spec 016 測試基線：P/C 增量 upsert 不重複、回填退路、走 taifex 限流、
訓練/serving 特徵同定義、恐慌分位計算可重現。point-in-time 另見 tests/test_pc_alignment.py。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from data_sources import taifex_loader as tx
from processor import market_regime as mr


@pytest.fixture
def pcr(tmp_path, monkeypatch):
    monkeypatch.setattr(tx, "_DIR", tmp_path)
    monkeypatch.setattr(tx, "PCR_PATH", tmp_path / "pcr.parquet")
    return tmp_path / "pcr.parquet"


def _frame(days, oi):
    rows = [tx._row(d, 1, 1, 100, 1, 1) | {"pc_oi_ratio": v} for d, v in zip(days, oi)]
    return pd.DataFrame(rows, columns=tx.COLUMNS)


# ── T009 refresh_recent：重疊窗 upsert 不產生重複列，新值覆蓋舊值 ──

def test_refresh_recent_upserts_without_duplicates(pcr, monkeypatch):
    first = _frame(pd.bdate_range("2026-09-01", periods=10).date, [100.0] * 10)
    second = _frame(pd.bdate_range("2026-09-08", periods=10).date, [150.0] * 10)   # 與前一批重疊 5 天
    batches = iter([first, second])
    monkeypatch.setattr(tx, "fetch_pc_recent", lambda: next(batches))
    tx.refresh_recent()
    out = tx.refresh_recent()
    df = tx.read_pcr()
    assert out["total"] == 15 and len(df) == 15
    assert df["trade_date"].is_unique and df["trade_date"].is_monotonic_increasing
    assert df.loc[df["trade_date"] == pd.Timestamp("2026-09-08"), "pc_oi_ratio"].item() == 150.0


def test_refresh_failure_is_guarded(pcr, monkeypatch):
    def boom():
        raise ConnectionError("taifex down")

    monkeypatch.setattr(tx, "fetch_pc_recent", boom)
    assert tx.refresh_recent() == {"refreshed": False, "reason": "taifex down"}


# ── T010 backfill：CSV/HTML 夠多就用；不足則退回 OpenAPI seed ──

def test_backfill_uses_history_when_available(pcr, monkeypatch):
    hist = _frame(pd.bdate_range("2025-01-01", periods=300).date, [100.0] * 300)
    monkeypatch.setattr(tx, "fetch_pc_history", lambda s, e: hist)
    monkeypatch.setattr(tx, "fetch_pc_recent", lambda: pytest.fail("不該退回 OpenAPI"))
    out = tx.backfill()
    assert out["source"] == "csv" and out["total"] == 300


def test_backfill_falls_back_to_openapi_seed(pcr, monkeypatch):
    monkeypatch.setattr(tx, "fetch_pc_history", lambda s, e: _frame([date(2026, 9, 1)], [100.0]))
    seed = _frame(pd.bdate_range("2026-09-01", periods=15).date, [120.0] * 15)
    monkeypatch.setattr(tx, "fetch_pc_recent", lambda: seed)
    out = tx.backfill()
    assert out["source"] == "openapi_seed" and out["total"] == 15


def test_parse_pc_html_reads_unpadded_dates_and_commas():
    html = ("<table><tr><th>日期</th></tr>"
            "<tr><td>2026/6/8</td><td>1,234</td><td>2,000</td><td>61.7</td>"
            "<td>10,000</td><td>8,000</td><td>125.0</td></tr>"
            "<tr><td>小計</td><td>1</td><td>1</td><td>1</td><td>1</td><td>1</td><td>1</td></tr></table>")
    rows = tx._parse_pc_html(html)
    assert len(rows) == 1
    r = rows[0]
    assert r["trade_date"] == pd.Timestamp("2026-06-08")
    assert (r["put_vol"], r["pc_vol_ratio"], r["pc_oi_ratio"]) == (1234.0, 61.7, 125.0)


# ── T011 所有抓取都先過 rate_limiter 的 taifex bucket ──

class _Resp:
    def __init__(self, body=None, text=""):
        self._body, self.content = body or [], text.encode()

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_openapi_fetch_goes_through_taifex_bucket(monkeypatch):
    seen = []
    monkeypatch.setattr(tx.rate_limiter, "acquire", lambda p, **k: seen.append(p))
    monkeypatch.setattr(tx.httpx, "get", lambda *a, **k: _Resp([
        {"Date": "20260908", "PutVolume": "1", "CallVolume": "1", "PutCallVolumeRatio%": "100",
         "PutOI": "1", "CallOI": "1", "PutCallOIRatio%": "110"},
        {"Date": "bad"}]))
    df = tx.fetch_pc_recent()
    assert seen == ["taifex"] and len(df) == 1 and df["pc_oi_ratio"].item() == 110.0


def test_history_fetch_acquires_once_per_chunk(monkeypatch):
    seen = []
    monkeypatch.setattr(tx.rate_limiter, "acquire", lambda p, **k: seen.append(p))

    class Client:
        def __init__(self, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def get(self, url):
            return _Resp()

        def post(self, url, data=None):
            return _Resp(text="")

    monkeypatch.setattr(tx.httpx, "Client", Client)
    tx.fetch_pc_history(date(2026, 1, 1), date(2026, 2, 14))         # 45 天 → 3 塊（14 天一塊）
    assert seen == ["taifex"] * 3


# ── T012 訓練端（pc_feature_frame）與 serving 端（latest_pc_features）同定義 ──

def test_serving_features_equal_training_row_of_previous_day(monkeypatch):
    rng = np.random.default_rng(0)
    days = pd.bdate_range("2026-06-01", periods=60)
    raw = _frame(days.date, list(100 + rng.normal(0, 10, 60)))
    monkeypatch.setattr(tx, "read_pcr", lambda: raw.copy())
    frame = mr.pc_feature_frame()
    assert list(frame.columns) == ["trade_date", "known_date", *mr.PC_FEATURES]
    today = days[45]
    served = mr.latest_pc_features(now=today)
    yesterday = frame[frame["trade_date"] == days[44]].iloc[0]
    assert set(served) == set(mr.PC_FEATURES)
    for f in mr.PC_FEATURES:
        assert served[f] == pytest.approx(round(float(yesterday[f]), 4))


def test_z_score_and_change_definitions():
    oi = [100.0] * 19 + [130.0]
    df = mr._enrich(_frame(pd.bdate_range("2026-06-01", periods=20).date, oi))
    last = df.iloc[-1]
    s = pd.Series(oi)
    assert last["pc_oi_z20"] == pytest.approx((130 - s.mean()) / s.std())
    assert last["pc_oi_chg5"] == 30.0


# ── T013 恐慌分位：當前 z 在歷史中的百分位、可重現、asof 截斷 ──

@pytest.fixture
def fear_frame(monkeypatch):
    days = pd.bdate_range("2026-01-01", periods=60)
    z = list(np.linspace(-2, 2, 59)) + [0.0]                          # 最後一天 z=0（居中）
    frame = pd.DataFrame({"trade_date": days, "known_date": days, "pc_oi_z20": z,
                          "pc_oi_ratio": 1.0, "pc_vol_ratio": 1.0, "pc_oi_chg5": 0.0})
    monkeypatch.setattr(mr, "pc_feature_frame", lambda: frame.copy())
    return days


def test_fear_score_is_percentile_of_current_z(fear_frame):
    score = mr.market_fear_score()
    assert score == pytest.approx(round(31 / 60, 4))                  # ≤0 的有 30 個 + 自己
    assert mr.market_fear_score() == score                            # 可重現


def test_fear_score_asof_only_uses_history_up_to_that_day(fear_frame):
    asof = fear_frame[39].date().isoformat()                          # 那天 z 是上升序列的第 40 個＝最高
    assert mr.market_fear_score(asof) == 1.0


def test_fear_score_needs_30_days(fear_frame):
    assert mr.market_fear_score(fear_frame[20].date().isoformat()) is None
