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
        {"date": "2026-06-12", "before_price": 100.0, "after_price": 0.0},       # 非正 丟
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


# ── 讀取端還原（WP1.2 / US2）──

def _raw(dates, closes):
    d = pd.to_datetime(dates)
    return pd.DataFrame({
        "trade_date": d, "open": closes, "high": closes, "low": closes,
        "close": [float(c) for c in closes],
        "volume": [1] * len(closes), "amount": [1e8] * len(closes), "source": "t",
    })[local_store.PRICE_COLUMNS]


def test_apply_adjustment_removes_ex_gap(tmp_adj_path):
    """已知除息日：原始 -10% 假跳空 → 還原後跨除息日報酬連續（≈0）（SC-003）。"""
    local_store.write_adj_factors(_df([
        {"symbol": "X", "ex_date": pd.Timestamp("2024-01-04"), "adj_factor": 0.9, "source": "finmind"},
    ]))
    raw = _raw(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"], [100, 100, 90, 90])
    adj = local_store._apply_adjustment(raw, "X")
    # ex_date 前 ×0.9、ex_date 當日及之後維持原始 → 全部 90，跨除息日連續
    assert [round(c, 6) for c in adj["close"]] == [90.0, 90.0, 90.0, 90.0]
    r = adj["close"].pct_change()
    assert abs(r.iloc[2]) < 1e-9                 # 原始 90/100-1=-10% 假跳空 → 還原後 0
    assert list(adj["volume"]) == list(raw["volume"])   # volume 不還原


def test_apply_adjustment_multi_ex_cumulative(tmp_adj_path):
    """多次除權息：ex_date 前累乘所有後續因子（backward 累積）。"""
    local_store.write_adj_factors(_df([
        {"symbol": "X", "ex_date": pd.Timestamp("2024-01-04"), "adj_factor": 0.9, "source": "finmind"},
        {"symbol": "X", "ex_date": pd.Timestamp("2024-01-08"), "adj_factor": 0.8, "source": "finmind"},
    ]))
    raw = _raw(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"],
               [100, 100, 90, 90, 72, 72])
    adj = local_store._apply_adjustment(raw, "X")
    # d0/d1 ×(0.9·0.8)=0.72、d2/d3 ×0.8、d4/d5 ×1 → 全部 72，跨兩除息日皆連續
    assert [round(c, 4) for c in adj["close"]] == [72.0, 72.0, 72.0, 72.0, 72.0, 72.0]


def test_read_prices_adjusted_flag(tmp_adj_path, tmp_path, monkeypatch):
    """read_prices(adjusted=True) 套用還原；預設 False 回原始價。"""
    monkeypatch.setattr(local_store, "PARQUET_ROOT", tmp_path)
    (tmp_path / "tw").mkdir()
    raw = _raw(["2024-01-02", "2024-01-03", "2024-01-04"], [100, 100, 90])
    raw.to_parquet(tmp_path / "tw" / "X.parquet", index=False)
    local_store.write_adj_factors(_df([
        {"symbol": "X", "ex_date": pd.Timestamp("2024-01-04"), "adj_factor": 0.9, "source": "finmind"},
    ]))
    assert list(local_store.read_prices("X", "tw")["close"]) == [100.0, 100.0, 90.0]          # 原始
    assert [round(c, 4) for c in local_store.read_prices("X", "tw", adjusted=True)["close"]] == [90.0, 90.0, 90.0]


def test_apply_adjustment_no_factors_noop(tmp_adj_path):
    """無因子（指數/ETF/不配息）→ 原樣返回。"""
    raw = _raw(["2024-01-02", "2024-01-03"], [100, 101])
    adj = local_store._apply_adjustment(raw, "NOFACTOR")
    assert list(adj["close"]) == [100.0, 101.0]


# ── serving 端雙軌：顯示 close 原始、報酬/波動用還原（WP1.2 / T012, T015）──

def test_price_block_display_raw_return_adjusted():
    """_price_block：顯示 close = 原始名目價；return_1d 用還原價（除息日不再假跳空）。"""
    from processor import tw_features

    dates = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    raw = _raw(dates, [100, 100, 100, 100, 90])          # 最後一日除息 -10% 假跳空
    adj = _raw(dates, [90, 90, 90, 90, 90])              # read_prices(adjusted=True) 的等效結果
    block = tw_features._price_block(raw, adj)
    assert block["close"] == 90.0                        # 顯示＝原始名目最後收盤
    assert block["return_1d_pct"] == 0.0                 # 還原後跨除息日連續（非原始 -10%）


def test_price_block_no_adj_uses_raw():
    """df_adj=None → 退回原始（向後相容，與訓練端 adjusted=True 缺因子時一致）。"""
    from processor import tw_features

    raw = _raw(["2024-01-02", "2024-01-03"], [100, 110])
    block = tw_features._price_block(raw)
    assert block["close"] == 110.0
    assert block["return_1d_pct"] == 10.0


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


# ── spec 020：股利事件特徵（只回看，point-in-time）──────────────────────

def _factors(*rows):
    return pd.DataFrame(rows, columns=["ex_date", "adj_factor"]).assign(symbol="2330", source="finmind")


def test_dividend_features_only_look_back():
    from processor.adj_factors import dividend_features

    factors = _factors(("2026-03-10", 0.98), ("2026-06-10", 0.96))
    dates = pd.to_datetime(["2026-03-09", "2026-03-10", "2026-03-20", "2026-06-10", "2027-08-01"])
    out = dividend_features(factors, pd.Series(dates))
    # 除息日前一天：事件尚未發生，兩個特徵都必須是空值（不得偷看隔天的除息）
    assert out.iloc[0].isna().all()
    assert out.loc[1, "days_since_ex_div"] == 0
    assert out.loc[2, "days_since_ex_div"] == 10
    assert out.loc[2, "div_yield_ttm_pct"] == pytest.approx(round((1 / 0.98 - 1) * 100, 2))
    # 一年內兩次事件要合計
    assert out.loc[3, "div_yield_ttm_pct"] == pytest.approx(
        round(((1 / 0.98 - 1) + (1 / 0.96 - 1)) * 100, 2), abs=0.01)
    # 超過一年沒除息：天數封頂、殖利率回到空值（不是 0）
    assert out.loc[4, "days_since_ex_div"] == 365
    assert pd.isna(out.loc[4, "div_yield_ttm_pct"])


def test_dividend_features_empty_for_never_paid():
    from processor.adj_factors import dividend_features

    out = dividend_features(_factors(), pd.Series(pd.to_datetime(["2026-03-09"])))
    assert out.isna().all().all()


def test_future_ex_date_does_not_change_past_features():
    """break-proof：往因子表加一筆未來事件，過去日期的特徵不得改變。"""
    from processor.adj_factors import dividend_features

    dates = pd.Series(pd.to_datetime(["2026-03-20", "2026-04-20"]))
    base = dividend_features(_factors(("2026-03-10", 0.98)), dates)
    more = dividend_features(_factors(("2026-03-10", 0.98), ("2026-05-01", 0.9)), dates)
    pd.testing.assert_frame_equal(base, more)



# ── spec 018：大比例配股 / 分割 / 減資（範圍外的因子須經股價驗證）──────────

def _ohlc(rows):
    """rows: (date, open, close)。"""
    d = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({
        "trade_date": d, "open": [float(r[1]) for r in rows], "high": [float(r[2]) for r in rows],
        "low": [float(r[2]) for r in rows], "close": [float(r[2]) for r in rows],
        "volume": 1, "amount": 1e8, "source": "t",
    })[local_store.PRICE_COLUMNS]


def _row(sym, day, factor, source=adj_factors.SOURCE_DIVIDEND):
    return {"symbol": sym, "ex_date": pd.Timestamp(day), "adj_factor": factor, "source": source}


def test_wide_factor_requires_opt_in():
    """預設仍只收一般配息範圍；wide=True 才回範圍外的值（呼叫端須再驗證）。"""
    assert adj_factors.compute_factor(7800.0, 2614.99) is None
    assert adj_factors.compute_factor(7800.0, 2614.99, wide=True) == pytest.approx(0.335255, abs=1e-6)
    assert adj_factors.compute_factor(6.6, 23.86, wide=True) == pytest.approx(3.615152, abs=1e-6)
    assert adj_factors.compute_factor(100.0, 0.05, wide=True) is None         # 超出絕對界線


def test_large_stock_dividend_is_kept_when_prices_agree():
    """6669 2026-09-02 的實例：7,800 → 參考價 2,614.99，當日開 2,615。舊下限 0.5 會把它丟掉。"""
    prices = _ohlc([("2026-08-31", 7095, 7095), ("2026-09-01", 7800, 7800), ("2026-09-02", 2615, 2610)])
    rows = adj_factors.rows_from_results("6669", [
        {"date": "2026-09-02", "before_price": 7800.0, "after_price": 2614.99}])
    kept = adj_factors.confirm_with_prices(rows, prices)
    assert [r["adj_factor"] for r in kept] == [pytest.approx(0.335255, abs=1e-6)]


def test_out_of_range_factor_is_dropped_when_prices_disagree():
    """來源填錯（或事件其實沒發生）：股價沒有對應的跳動，就不得把大倍率套到整段歷史。"""
    flat = _ohlc([("2026-09-01", 100, 100), ("2026-09-02", 101, 102)])
    assert adj_factors.confirm_with_prices([_row("X", "2026-09-02", 0.3)], flat) == []
    assert adj_factors.confirm_with_prices([_row("X", "2026-09-02", 3.0)], flat) == []


def test_out_of_range_factor_waits_for_prices():
    """事件日行情還沒入庫：先不寫，下一輪再驗（fail-closed）。"""
    before_only = _ohlc([("2026-09-01", 100, 100)])
    assert adj_factors.confirm_with_prices([_row("X", "2026-09-02", 0.3)], before_only) == []


def test_ordinary_dividend_needs_no_price_check():
    empty = _ohlc([])
    assert len(adj_factors.confirm_with_prices([_row("X", "2026-09-02", 0.97)], empty)) == 1


def test_split_and_reduction_rows():
    splits = adj_factors.rows_from_splits([
        {"date": "2026-09-07", "stock_id": "6949", "type": "面額變更",
         "before_price": 1490.0, "after_price": 74.5}])
    assert (splits[0]["symbol"], splits[0]["source"]) == ("6949", adj_factors.SOURCE_SPLIT)
    assert splits[0]["adj_factor"] == pytest.approx(0.05)
    red = adj_factors.rows_from_reductions("2380", [
        {"date": "2026-06-29", "ClosingPriceonTheLastTradingDay": 6.6,
         "PostReductionReferencePrice": 23.86}])
    assert red[0]["source"] == adj_factors.SOURCE_REDUCTION
    assert red[0]["adj_factor"] == pytest.approx(23.86 / 6.6, abs=1e-6)


def test_split_and_reduction_make_returns_continuous(tmp_adj_path):
    """因子 <0.5（分割）與 >1（減資）都要讓跨事件日的報酬連續。"""
    local_store.write_adj_factors(_df([
        _row("S", "2024-01-04", 0.1, adj_factors.SOURCE_SPLIT),
        _row("R", "2024-01-04", 3.0, adj_factors.SOURCE_REDUCTION),
    ]))
    split = local_store._apply_adjustment(
        _raw(["2024-01-02", "2024-01-03", "2024-01-04"], [1000, 1000, 100]), "S")
    assert abs(split["close"].pct_change().iloc[2]) < 1e-9
    reduction = local_store._apply_adjustment(
        _raw(["2024-01-02", "2024-01-03", "2024-01-04"], [6, 6, 18]), "R")
    assert abs(reduction["close"].pct_change().iloc[2]) < 1e-9


def test_same_day_events_from_different_sources_coexist(tmp_adj_path):
    """除息與減資同一天：兩筆都要留、還原時連乘；同來源重寫才覆蓋。"""
    local_store.write_adj_factors(_df([_row("X", "2024-01-04", 0.95)]))
    local_store.write_adj_factors(_df([_row("X", "2024-01-04", 2.0, adj_factors.SOURCE_REDUCTION)]))
    local_store.write_adj_factors(_df([_row("X", "2024-01-04", 0.96)]))          # 同來源 → 覆蓋
    table = local_store.read_adj_factors("X")
    assert sorted(table["adj_factor"]) == [0.96, 2.0]
    adj = local_store._apply_adjustment(_raw(["2024-01-03", "2024-01-04"], [100, 192]), "X")
    assert adj["close"].iloc[0] == pytest.approx(100 * 0.96 * 2.0)


def test_dividend_features_ignore_splits_and_reductions():
    from processor.adj_factors import dividend_features

    factors = pd.DataFrame([
        _row("X", "2026-03-10", 0.98),
        _row("X", "2026-04-10", 0.1, adj_factors.SOURCE_SPLIT),
        _row("X", "2026-05-10", 0.335),                                # 大比例配股不算殖利率
    ])
    out = dividend_features(factors, pd.Series(pd.to_datetime(["2026-06-01"])))
    assert out.loc[0, "div_yield_ttm_pct"] == pytest.approx(round((1 / 0.98 - 1) * 100, 2))
    assert out.loc[0, "days_since_ex_div"] == 83


def test_find_unexplained_jumps(tmp_adj_path, monkeypatch):
    days = pd.bdate_range("2024-01-01", periods=15)
    closes = [100.0] * 12 + [30.0, 30.0, 31.0]                         # 第 13 根 -70%
    monkeypatch.setattr(local_store, "read_prices",
                        lambda sym, market, adjusted=False: _raw(days, closes))
    jumps = adj_factors.find_unexplained_jumps(symbols=["X"])
    assert jumps == [{"symbol": "X", "date": days[12].date().isoformat(), "return_pct": -70.0}]
    assert adj_factors.find_unexplained_jumps(symbols=["X"], since="2025-01-01") == []
