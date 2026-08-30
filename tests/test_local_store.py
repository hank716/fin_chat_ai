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


# ── parquet 損毀韌性（2026-08-30 事故迴歸）──────────────────────────────────────
# 容器 rebuild 把 to_parquet 砍在半路 → tw/_margin/5530.parquet 被截成沒有結尾 magic bytes
# 的死檔，讀它 ArrowInvalid 炸掉整個 build_tw_features（＝晨報），而寫入端讀舊檔那行也炸，
# 壞檔連覆寫自救都做不到。下列測試釘住「寫入原子 + 讀到壞檔自癒」兩道保險。

def _truncated_parquet(path):
    """造一個「有 PAR1 開頭、沒 PAR1 結尾」的半截檔——與事故現場的位元組特徵相同。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    good = pd.DataFrame({"trade_date": pd.to_datetime(["2024-01-01"]), "close": [1.0]})
    good.to_parquet(path, index=False)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])          # 砍掉後半＝footer 不見
    return path


def test_read_corrupt_quarantines_and_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "PARQUET_ROOT", tmp_path)
    p = _truncated_parquet(tmp_path / "tw" / "_margin" / "5530.parquet")

    df = ls.read_margin("5530", "tw")

    assert df.empty and list(df.columns) == ls.MARGIN_COLUMNS   # 回空、欄位仍正確
    assert not p.exists()                                        # 壞檔已被移走
    assert list(p.parent.glob("5530.parquet.corrupt-*"))         # 留證可鑑識


def test_upsert_over_corrupt_recovers(tmp_path):
    """解死結：壞檔存在時 _upsert_parquet 不得拋例外，且新資料要完整落地。"""
    p = _truncated_parquet(tmp_path / "S.parquet")

    n = ls._upsert_parquet(p, _df(["2024-02-01", "2024-02-02"], [20, 21]))

    assert n == 2
    out = pd.read_parquet(p)
    assert list(out["trade_date"]) == list(pd.to_datetime(["2024-02-01", "2024-02-02"]))


def test_write_by_symbol_survives_one_corrupt_symbol(tmp_path, monkeypatch):
    """單一壞檔不得中斷整批 symbol 的落地（事故當下 write_margin 全市場會停在 5530）。"""
    monkeypatch.setattr(ls, "PARQUET_ROOT", tmp_path)
    _truncated_parquet(tmp_path / "tw" / "_margin" / "5530.parquet")

    from dataclasses import dataclass

    @dataclass
    class _Row:
        symbol: str
        trade_date: date
        margin_balance: float
        short_balance: float
        source: str = "t"

    d = date(2024, 3, 1)
    res = ls.write_margin([_Row("5530", d, 1.0, 2.0), _Row("2330", d, 3.0, 4.0)], "tw")

    assert res["symbols"] == 2                                   # 兩檔都寫成功
    assert len(ls.read_margin("5530", "tw")) == 1


def test_atomic_write_leaves_no_tmp(tmp_path):
    p = tmp_path / "S.parquet"
    ls.write_parquet_atomic(_df(["2024-01-01"], [10]), p)
    assert len(pd.read_parquet(p)) == 1
    assert not list(tmp_path.glob("*.tmp-*"))                    # 沒有殘留 tmp


def test_atomic_write_keeps_old_version_on_failure(tmp_path):
    """寫入中途失敗時，目的檔必須維持「完整舊版」，不能被截成半截檔。"""
    p = tmp_path / "S.parquet"
    ls.write_parquet_atomic(_df(["2024-01-01"], [10]), p)

    class _Boom(pd.DataFrame):
        def to_parquet(self, *a, **k):
            raise RuntimeError("killed mid-write")

    with pytest.raises(RuntimeError):
        ls.write_parquet_atomic(_Boom(_df(["2024-01-02"], [11])), p)

    out = pd.read_parquet(p)                                     # 舊版完好可讀
    assert len(out) == 1 and out.iloc[0]["close"] == 10
    assert not list(tmp_path.glob("*.tmp-*"))


def test_scan_corrupt_reports_then_quarantines(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "PARQUET_ROOT", tmp_path)
    _df(["2024-01-01"], [10]).to_parquet(tmp_path / "good.parquet", index=False)
    bad = _truncated_parquet(tmp_path / "tw" / "bad.parquet")

    report = ls.scan_corrupt_parquet()                           # 預設唯讀：只報告不動檔
    assert report["files_scanned"] == 2
    assert [c["path"] for c in report["corrupt"]] == [str(bad)]
    assert report["quarantined"] == 0 and bad.exists()

    assert ls.scan_corrupt_parquet(quarantine=True)["quarantined"] == 1
    assert not bad.exists()
    assert ls.scan_corrupt_parquet()["corrupt"] == []            # 冪等：再掃已乾淨
