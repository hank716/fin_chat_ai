"""Intermarket features（M1 step 2）。

讀 Step 1 落地的 US 指數 + BTC parquet，算：
  - 每資產：最新收盤、1d/5d/20d 報酬、20d 波動、是否站上 MA20
  - 跨市場：對齊交易日後的日報酬相關性（BTC↔Nasdaq↔SOX↔S&P）

輸出純 dict（JSON-safe），作為 Step 3 餵 Gemini 的結構化輸入。
不做 AI 判讀；只算數字。NaN 一律轉 None。
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from storage import local_store

# (本系統 symbol, market, 顯示名)
ASSETS: list[tuple[str, str, str]] = [
    ("SP500", "us", "S&P 500"),
    ("NASDAQ", "us", "Nasdaq Composite"),
    ("DJI", "us", "Dow Jones"),
    ("SOX", "us", "費城半導體"),
    ("BTC", "crypto", "Bitcoin"),
    ("ETH", "crypto", "Ethereum"),
    ("SOL", "crypto", "Solana"),
]

CORR_PAIRS = [
    ("BTC", "NASDAQ"), ("BTC", "SOX"), ("SOX", "NASDAQ"), ("SP500", "NASDAQ"),
    ("ETH", "BTC"), ("ETH", "NASDAQ"), ("SOL", "BTC"),
]


def _clean(v: Any) -> Any:
    """float NaN/inf → None；numpy 數值 → python 原生。"""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _pct(v: Any, ndigits: int = 2) -> float | None:
    v = _clean(float(v) if v is not None else None)
    return round(v * 100, ndigits) if v is not None else None


def _cum_return_pct(close: pd.Series, n: int) -> float | None:
    if len(close) <= n:
        return None
    prev = close.iloc[-1 - n]
    if prev == 0 or pd.isna(prev):
        return None
    return round((close.iloc[-1] / prev - 1) * 100, 2)


def build_intermarket_features(window: int = 20) -> dict[str, Any]:
    series: dict[str, pd.DataFrame] = {}
    for sym, market, _name in ASSETS:
        df = local_store.read_prices(sym, market)
        if df.empty:
            continue
        df = df.sort_values("trade_date").reset_index(drop=True)
        df["ret"] = df["close"].pct_change()
        series[sym] = df

    assets_out: dict[str, Any] = {}
    latest_dates: list[pd.Timestamp] = []
    for sym, market, name in ASSETS:
        df = series.get(sym)
        if df is None or df.empty:
            continue
        close = df["close"]
        ret = df["ret"]
        last = df.iloc[-1]
        latest_dates.append(last["trade_date"])
        ma20 = close.tail(20).mean()
        assets_out[sym] = {
            "name": name,
            "market": market,
            "as_of": last["trade_date"].date().isoformat(),
            "close": round(float(last["close"]), 2),
            "return_1d_pct": _pct(ret.iloc[-1]),
            "return_5d_pct": _cum_return_pct(close, 5),
            "return_20d_pct": _cum_return_pct(close, 20),
            "volatility_20d_pct": _clean(round(float(ret.tail(20).std()) * 100, 2))
            if len(ret.dropna()) >= 2 else None,
            "above_ma20": bool(last["close"] > ma20) if not pd.isna(ma20) else None,
        }

    # 跨市場相關性：對齊各資產日報酬（dropna = 共同交易日），取最近 window 天
    correlations: dict[str, Any] = {}
    if len(series) >= 2:
        ret_frame = pd.DataFrame(
            {sym: df.set_index("trade_date")["ret"] for sym, df in series.items()}
        ).dropna()
        ret_window = ret_frame.tail(window)
        if len(ret_window) >= 3:
            corr = ret_window.corr()
            for a, b in CORR_PAIRS:
                if a in corr.columns and b in corr.columns:
                    correlations[f"{a}~{b}"] = _clean(round(float(corr.loc[a, b]), 3))

    as_of = max(latest_dates).date().isoformat() if latest_dates else None
    return {
        "as_of": as_of,
        "window": window,
        "assets": assets_out,
        f"return_correlations_{window}d": correlations,
        "notes": "報酬相關性以共同交易日對齊計算；BTC 含週末交易，已對齊到美股交易日。",
    }
