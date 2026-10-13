"""spec 018 US4：上市行情的缺日 / 錯置日 / 幽靈日偵測，與嚴格日期檢查。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from data_sources import backfill_tw_market as bf
from data_sources import price_integrity as pi
from storage import local_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "local_parquet"
    (root / "tw").mkdir(parents=True)
    monkeypatch.setattr(local_store, "PARQUET_ROOT", root)
    monkeypatch.setattr(pi, "PARQUET_ROOT", root)
    monkeypatch.setattr(pi, "REPORT_PATH", root / "_price_integrity.json")
    return root


def _write(root, symbol, days, source):
    df = pd.DataFrame({"trade_date": pd.to_datetime(days), "open": 1.0, "high": 1.0, "low": 1.0,
                       "close": 1.0, "volume": 1, "amount": 1e8, "source": source})
    df[local_store.PRICE_COLUMNS].to_parquet(root / "tw" / f"{symbol}.parquet", index=False)


def _calendar(n=12):
    return [d.date().isoformat() for d in pd.bdate_range("2026-03-02", periods=n)]


def test_strict_date_discards_mislabeled_response(monkeypatch):
    """請求 03-04 卻回 03-18：整批丟棄，不得以回應日期落地。"""
    monkeypatch.setattr(bf, "_dq_filter", lambda rows: rows)
    rows = [SimpleNamespace(symbol="3189")]
    mislabeled = lambda d: (date(2026, 3, 18), rows)          # noqa: E731
    correct = lambda d: (d, rows)                             # noqa: E731
    assert bf._try(mislabeled, date(2026, 3, 4)) == []
    assert bf._try(mislabeled, date(2026, 3, 4), strict=False) != []     # 只回最新一日的端點才放寬
    assert bf._try(correct, date(2026, 3, 4)) != []


def test_diagnose_finds_missing_and_ghost_days(store):
    days = _calendar()
    for sym in ("8001", "8002"):                              # 上櫃：完整日曆
        _write(store, sym, days, "finmind")
    for i in range(10):                                       # 上市：缺一天、多一個週日
        listed_days = [d for d in days if d != days[5]] + ["2026-03-08"]
        _write(store, f"11{i:02d}", listed_days, "twse")
    found = pi.diagnose()
    assert found["missing_dates"] == [days[5]]
    assert found["ghost_dates"] == ["2026-03-08"]


def test_suspect_days_need_a_cluster_of_jumps(store):
    days = _calendar()
    _write(store, "8001", days, "finmind")
    for i in range(10):
        _write(store, f"11{i:02d}", days, "twse")
    one = [{"symbol": "1100", "date": days[6], "return_pct": -40.0}]
    assert pi.diagnose(jumps=one)["suspect_dates"] == []      # 單檔跳動多半是真事件
    warrants = [{"symbol": f"71113{i}", "date": days[6], "return_pct": 90.0} for i in range(9)]
    assert pi.diagnose(jumps=warrants)["suspect_dates"] == []  # 權證不受 ±10% 限制，不算數
    cluster = [{"symbol": f"11{i:02d}", "date": days[6], "return_pct": -40.0} for i in range(5)]
    # 壞的可能是當天，也可能是前一天（跳過去又跳回來），兩天都重抓
    assert pi.diagnose(jumps=cluster)["suspect_dates"] == [days[5], days[6]]


def test_purge_only_touches_named_dates(store):
    days = _calendar(5) + ["2026-03-08"]
    _write(store, "1101", days, "twse")
    assert pi.purge_dates(["2026-03-08"]) == {"files_modified": 1, "rows_removed": 1}
    left = pd.read_parquet(store / "tw" / "1101.parquet")
    assert len(left) == 5 and pd.Timestamp("2026-03-08") not in set(pd.to_datetime(left["trade_date"]))
    assert pi.purge_dates([]) == {"files_modified": 0, "rows_removed": 0}
