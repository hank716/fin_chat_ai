"""WP1.1 除權息因子表測試（spec 017）。

純確定性：因子公式 + sanity 邊界、結果列轉換過濾、(symbol,ex_date) upsert 冪等、
build() 斷點續跑與遇 quota/ban 停手。不打任何外部 API。
"""
from __future__ import annotations

import pandas as pd
import pytest

from processor import adj_factors
from storage import local_store


# ── 因子公式 + sanity ──

def test_compute_factor_known_cases():
    """3 個 2330 真實除權息參考價 → 手算誤差 <0.1%（驗收案例）。"""
    # (before, after, 期望 factor)：TaiwanStockDividendResult 2330 實際值
    cases = [
        (1505.0, 1499.99, 1499.99 / 1505.0),   # 2025-12-11 每季配息 5 元
        (1845.0, 1838.99, 1838.99 / 1845.0),    # 2026-03-17 配息 6 元
        (2255.0, 2248.99, 2248.99 / 2255.0),    # 2026-06-11 配息 6 元
    ]
    for before, after, expected in cases:
        f = adj_factors.compute_factor(before, after)
        assert f is not None
        assert abs(f - expected) / expected < 0.001   # <0.1%


def test_compute_factor_high_dividend_yield():
    """高配息（如 0056 類）因子明顯 <1 但仍 >0.5，通過 sanity。"""
    f = adj_factors.compute_factor(35.0, 32.5)   # ~7% 配息參考價下修
    assert f is not None
    assert 0.9 < f < 0.95


def test_compute_factor_stock_dividend_near_lower_bound():
    """含配股（除權）因子可較低，但仍須 >0.5 才收。"""
    assert adj_factors.compute_factor(100.0, 60.0) == pytest.approx(0.6, abs=1e-6)
    assert adj_factors.compute_factor(100.0, 50.0) is None   # 恰 0.5 → 落在 (0.5,1.0] 外，丟棄
    assert adj_factors.compute_factor(100.0, 49.0) is None   # <0.5 → 異常，丟棄


@pytest.mark.parametrize("before,after", [
    (100.0, 101.0),    # after>before → factor>1，除權息不可能，sanity 丟棄
    (0.0, 50.0),       # before 非正
    (100.0, 0.0),      # after 非正
    (100.0, -5.0),     # 負值
    (None, 50.0),      # 缺值
    ("x", "y"),        # 非數值
])
def test_compute_factor_rejects_bad(before, after):
    assert adj_factors.compute_factor(before, after) is None


# ── 結果列轉換 ──

def test_rows_from_results_filters_and_shapes():
    raw = [
        {"date": "2026-06-11", "before_price": 2255.0, "after_price": 2248.99},  # ok
        {"date": "2026-06-12", "before_price": 100.0, "after_price": 101.0},     # factor>1 丟
        {"date": None, "before_price": 100.0, "after_price": 90.0},              # 無日期 丟
        {"date": "2026-06-13", "before_price": None, "after_price": 90.0},       # 缺 before 丟
    ]
    rows = adj_factors.rows_from_results("2330", raw)
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "2330"
    assert r["ex_date"] == pd.Timestamp("2026-06-11")
    assert r["source"] == "finmind"
    assert abs(r["adj_factor"] - 2248.99 / 2255.0) < 1e-6


# ── upsert 冪等 ──

@pytest.fixture
def tmp_adj_path(tmp_path, monkeypatch):
    p = tmp_path / "tw_adj_factors.parquet"
    monkeypatch.setattr(local_store, "ADJ_FACTORS_PATH", p)
    monkeypatch.setattr(adj_factors, "ADJ_FACTORS_PATH", p)
    return p


def _df(rows):
    return pd.DataFrame(rows, columns=local_store.ADJ_FACTORS_COLUMNS)


def test_write_read_roundtrip(tmp_adj_path):
    rows = [
        {"symbol": "2330", "ex_date": pd.Timestamp("2026-06-11"), "adj_factor": 0.9973, "source": "finmind"},
        {"symbol": "2330", "ex_date": pd.Timestamp("2026-03-17"), "adj_factor": 0.9967, "source": "finmind"},
    ]
    n = local_store.write_adj_factors(_df(rows))
    assert n == 2
    got = local_store.read_adj_factors("2330")
    assert list(got["ex_date"]) == [pd.Timestamp("2026-03-17"), pd.Timestamp("2026-06-11")]  # 依 ex_date 排序


def test_upsert_dedup_keep_last(tmp_adj_path):
    local_store.write_adj_factors(_df([
        {"symbol": "2330", "ex_date": pd.Timestamp("2026-06-11"), "adj_factor": 0.99, "source": "finmind"},
    ]))
    # 同 (symbol, ex_date) 重寫 → keep-last 覆蓋，不重複列
    total = local_store.write_adj_factors(_df([
        {"symbol": "2330", "ex_date": pd.Timestamp("2026-06-11"), "adj_factor": 0.9973, "source": "finmind"},
        {"symbol": "2454", "ex_date": pd.Timestamp("2026-07-01"), "adj_factor": 0.98, "source": "finmind"},
    ]))
    assert total == 2
    df = local_store.read_adj_factors()
    row = df[(df["symbol"] == "2330") & (df["ex_date"] == pd.Timestamp("2026-06-11"))]
    assert float(row["adj_factor"].iloc[0]) == pytest.approx(0.9973)


def test_read_missing_returns_empty(tmp_adj_path):
    df = local_store.read_adj_factors()
    assert df.empty
    assert list(df.columns) == local_store.ADJ_FACTORS_COLUMNS


# ── build() 斷點續跑 + 遇 backoff 停手 ──

def test_build_resumable_and_backoff_stop(tmp_adj_path, tmp_path, monkeypatch):
    from data_sources.finmind_loader import FinMindQuotaExceeded

    monkeypatch.setattr(adj_factors, "_DONE_PATH", tmp_path / "_done.json")
    monkeypatch.setattr(adj_factors, "_tw_symbols", lambda: ["AAA", "BBB", "CCC", "DDD"])

    calls: list[str] = []

    def fake_fetch(sym, start_date):
        calls.append(sym)
        if sym == "CCC":
            raise FinMindQuotaExceeded("quota")   # 第三檔額度耗盡 → 停手
        if sym == "AAA":
            return [{"symbol": "AAA", "ex_date": pd.Timestamp("2026-06-11"),
                     "adj_factor": 0.99, "source": "finmind"}]
        return []   # BBB 無股利

    monkeypatch.setattr(adj_factors, "_fetch_symbol", fake_fetch)

    res = adj_factors.build()
    assert res["stopped"] is not None
    assert res["processed"] == 2                 # AAA, BBB 完成；CCC 停手（不記 done）
    assert calls == ["AAA", "BBB", "CCC"]
    # DDD 尚未處理、CCC 因 backoff 未記 done → remaining 含兩者
    assert res["remaining"] == 2

    # 續跑：checkpoint 跳過 AAA/BBB，從 CCC 起（額度已回補）
    calls.clear()

    def fake_fetch_resume(sym, start_date):
        calls.append(sym)
        if sym in ("CCC", "DDD"):
            return []
        pytest.fail("重跑了已完成的檔 %s" % sym)

    monkeypatch.setattr(adj_factors, "_fetch_symbol", fake_fetch_resume)
    res2 = adj_factors.build()
    assert calls == ["CCC", "DDD"]
    assert res2["remaining"] == 0
    assert res2["stopped"] is None
