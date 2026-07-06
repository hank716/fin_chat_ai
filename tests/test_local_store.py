"""WP3.5：local_store parquet upsert 去重 + purge_future_rows。"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from storage import local_store as ls


def _df(dates, closes, source="a"):
    return pd.DataFrame({
        "trade_date": pd.to_datetime(dates),
        "close": [float(c) for c in closes],
        "source": [source] * len(dates),
    })


def test_upsert_dedup_keeps_last_and_sorts(tmp_path):
    p = tmp_path / "S.parquet"
    assert ls._upsert_parquet(p, _df(["2024-01-03", "2024-01-01"], [12, 10])) == 2
    # 重抓 01-01（新值 99）+ 新日 01-02 → 去重 keep-last、依日期排序
    n = ls._upsert_parquet(p, _df(["2024-01-01", "2024-01-02"], [99, 11], source="b"))
    assert n == 3
    out = pd.read_parquet(p)
    assert list(out["trade_date"]) == list(pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]))
    assert out.set_index("trade_date").loc[pd.Timestamp("2024-01-01"), "close"] == 99   # keep-last


def test_upsert_empty_is_noop(tmp_path):
    p = tmp_path / "S.parquet"
    assert ls._upsert_parquet(p, pd.DataFrame()) == 0
    assert not p.exists()


def test_purge_future_rows_removes_phantom(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "PARQUET_ROOT", tmp_path)
    mkt = tmp_path / "tw"
    mkt.mkdir()
    future = pd.Timestamp(date.today() + timedelta(days=10))
    _df([pd.Timestamp("2024-01-01"), future], [10, 999]).to_parquet(mkt / "S.parquet", index=False)
    res = ls.purge_future_rows("tw")
    assert res["rows_removed"] == 1 and res["files_modified"] == 1
    out = pd.read_parquet(mkt / "S.parquet")
    assert len(out) == 1
    assert pd.Timestamp(out.iloc[0]["trade_date"]) == pd.Timestamp("2024-01-01")


def test_purge_keeps_within_grace(tmp_path, monkeypatch):
    """今日 +1 天（在 2 天寬限內）不應被清（避免跨時區誤殺當日合法資料）。"""
    monkeypatch.setattr(ls, "PARQUET_ROOT", tmp_path)
    mkt = tmp_path / "tw"
    mkt.mkdir()
    near = pd.Timestamp(date.today() + timedelta(days=1))
    _df([pd.Timestamp("2024-01-01"), near], [10, 20]).to_parquet(mkt / "S.parquet", index=False)
    res = ls.purge_future_rows("tw")
    assert res["rows_removed"] == 0


def test_read_missing_returns_empty_with_columns():
    df = ls.read_prices("__nope__", "tw")
    assert df.empty and list(df.columns) == ls.PRICE_COLUMNS
