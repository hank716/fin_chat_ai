"""台股 features（M2-report step C）。

讀回補落地的台股價格 + 籌碼 parquet，算出給 AI 敘事/選股用的結構化數字：
  - index：大盤加權指數技術面
  - stocks：個股技術面（MA/報酬/波動）+ 相對大盤強弱 + 籌碼（三大法人淨買賣超、連續買超天數）
  - sectors：族群聚合（平均報酬、外資合計買超、領漲標的）
  - movers：漲跌幅 / 外資買賣超排行（方便 AI 點出候選觀察標的）

只算數字、不做 AI 判讀。NaN → None。籌碼單位輸出『張』（股/1000）便於閱讀。
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

import universe
from storage import local_store

TW_MARKET = "tw"
INDEX_SYMBOL = "TWII"


def _clean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _cum_return_pct(close: pd.Series, n: int) -> float | None:
    if len(close) <= n:
        return None
    prev = close.iloc[-1 - n]
    if prev == 0 or pd.isna(prev):
        return None
    return round((close.iloc[-1] / prev - 1) * 100, 2)


def _lots(shares: Any) -> int | None:
    """股 → 張（四捨五入）。"""
    if shares is None or pd.isna(shares):
        return None
    return int(round(float(shares) / 1000))


def _net_streak(series: pd.Series) -> int | None:
    """從最近一日往前數連續同向（買超為正、賣超為負）天數。

    回傳：正數=連續買超天數、負數=連續賣超天數、0=最新一日為 0 或無資料。
    """
    s = series.dropna()
    if s.empty:
        return None
    last = s.iloc[-1]
    if last == 0:
        return 0
    sign = 1 if last > 0 else -1
    streak = 0
    for v in reversed(s.tolist()):
        if (v > 0 and sign > 0) or (v < 0 and sign < 0):
            streak += 1
        else:
            break
    return streak * sign


def _price_block(df: pd.DataFrame) -> dict[str, Any] | None:
    if df.empty:
        return None
    df = df.sort_values("trade_date").reset_index(drop=True)
    close = df["close"]
    last = df.iloc[-1]
    ret = close.pct_change()
    ma20 = close.tail(20).mean()
    ma60 = close.tail(60).mean()
    return {
        "as_of": last["trade_date"].date().isoformat(),
        "close": round(float(last["close"]), 2),
        "return_1d_pct": _clean(round(float(ret.iloc[-1]) * 100, 2)) if len(ret.dropna()) else None,
        "return_5d_pct": _cum_return_pct(close, 5),
        "return_20d_pct": _cum_return_pct(close, 20),
        "volatility_20d_pct": _clean(round(float(ret.tail(20).std()) * 100, 2))
        if len(ret.dropna()) >= 2 else None,
        "above_ma20": bool(last["close"] > ma20) if not pd.isna(ma20) else None,
        "above_ma60": bool(last["close"] > ma60) if not pd.isna(ma60) else None,
        "_ret20_raw": _cum_return_pct(close, 20),  # 供相對強弱/族群聚合
    }


def _chip_block(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {}
    df = df.sort_values("trade_date").reset_index(drop=True)
    last = df.iloc[-1]
    foreign = df["foreign_net_buy"]
    return {
        "foreign_net_buy_1d_lots": _lots(last.get("foreign_net_buy")),
        "trust_net_buy_1d_lots": _lots(last.get("trust_net_buy")),
        "dealer_net_buy_1d_lots": _lots(last.get("dealer_net_buy")),
        "foreign_net_buy_5d_lots": _lots(foreign.tail(5).sum()),
        "foreign_net_streak": _net_streak(foreign),
        "trust_net_streak": _net_streak(df["trust_net_buy"]),
    }


def _margin_block(df: pd.DataFrame) -> dict[str, Any]:
    """融資融券：餘額(張)、融資增減、資券比(=融券餘額/融資餘額%)。"""
    if df.empty:
        return {}
    df = df.sort_values("trade_date").reset_index(drop=True)
    margin = df["margin_balance"]
    short = df["short_balance"]
    m_last = margin.iloc[-1]
    s_last = short.iloc[-1]
    m_prev = margin.iloc[-2] if len(margin) >= 2 else None
    m_5ago = margin.iloc[-6] if len(margin) >= 6 else None
    ratio = None
    if m_last and not pd.isna(m_last) and m_last != 0 and not pd.isna(s_last):
        ratio = round(float(s_last) / float(m_last) * 100, 2)
    return {
        "margin_balance_lots": _lots(m_last),
        "margin_chg_1d_lots": _lots(m_last - m_prev) if m_prev is not None and not pd.isna(m_prev) else None,
        "margin_chg_5d_lots": _lots(m_last - m_5ago) if m_5ago is not None and not pd.isna(m_5ago) else None,
        "short_balance_lots": _lots(s_last),
        "short_margin_ratio_pct": ratio,
    }


def build_tw_features(window: int = 20) -> dict[str, Any]:
    # 大盤
    index_df = local_store.read_prices(INDEX_SYMBOL, TW_MARKET)
    index_block = _price_block(index_df)
    index_ret20 = index_block["_ret20_raw"] if index_block else None
    if index_block:
        index_block.pop("_ret20_raw", None)
        index_block["name"] = universe.index_meta().get("name", "加權指數")

    stocks: dict[str, Any] = {}
    as_of_dates: list[str] = []
    for sym in sorted(universe.watchlist_symbols()):
        price = _price_block(local_store.read_prices(sym, TW_MARKET))
        if price is None:
            continue
        ret20 = price.pop("_ret20_raw", None)
        vs_index = (
            round(ret20 - index_ret20, 2)
            if ret20 is not None and index_ret20 is not None else None
        )
        entry = {
            "name": universe.display_name(sym),
            "sector": universe.sector_of(sym),
            **price,
            "vs_index_20d_pct": vs_index,
            **_chip_block(local_store.read_chip(sym, TW_MARKET)),
            **_margin_block(local_store.read_margin(sym, TW_MARKET)),
        }
        stocks[sym] = entry
        if price.get("as_of"):
            as_of_dates.append(price["as_of"])

    sectors = _aggregate_sectors(stocks)
    movers = _movers(stocks)
    as_of = max(as_of_dates) if as_of_dates else (index_block or {}).get("as_of")
    return {
        "as_of": as_of,
        "window": window,
        "index": index_block,
        "stocks": stocks,
        "sectors": sectors,
        "movers": movers,
        "notes": "籌碼單位為張(=股/1000)；vs_index_20d_pct=個股20日報酬減大盤20日報酬(相對強弱)；"
        "net_streak 正=連續買超天數、負=連續賣超天數。",
    }


def _aggregate_sectors(stocks: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sector, syms in universe.sectors().items():
        members = [(s, stocks[s]) for s in syms if s in stocks]
        if not members:
            continue
        ret20s = [e["return_20d_pct"] for _, e in members if e.get("return_20d_pct") is not None]
        ret5s = [e["return_5d_pct"] for _, e in members if e.get("return_5d_pct") is not None]
        foreign5 = [
            e["foreign_net_buy_5d_lots"] for _, e in members
            if e.get("foreign_net_buy_5d_lots") is not None
        ]
        leaders = sorted(
            (m for m in members if m[1].get("return_20d_pct") is not None),
            key=lambda m: m[1]["return_20d_pct"], reverse=True,
        )[:3]
        out[sector] = {
            "n": len(members),
            "avg_return_5d_pct": round(sum(ret5s) / len(ret5s), 2) if ret5s else None,
            "avg_return_20d_pct": round(sum(ret20s) / len(ret20s), 2) if ret20s else None,
            "foreign_net_buy_5d_lots": sum(foreign5) if foreign5 else None,
            "leaders": [
                {"symbol": s, "name": e["name"], "return_20d_pct": e["return_20d_pct"]}
                for s, e in leaders
            ],
        }
    return out


def _movers(stocks: dict[str, Any], top: int = 5) -> dict[str, Any]:
    def _rank(key: str, reverse: bool) -> list[dict[str, Any]]:
        items = [(s, e) for s, e in stocks.items() if e.get(key) is not None]
        items.sort(key=lambda x: x[1][key], reverse=reverse)
        return [
            {"symbol": s, "name": e["name"], "sector": e.get("sector"), key: e[key]}
            for s, e in items[:top]
        ]

    return {
        "top_gainers_5d": _rank("return_5d_pct", True),
        "top_losers_5d": _rank("return_5d_pct", False),
        "top_foreign_buy_5d": _rank("foreign_net_buy_5d_lots", True),
        "top_foreign_sell_5d": _rank("foreign_net_buy_5d_lots", False),
        "top_short_margin_ratio": _rank("short_margin_ratio_pct", True),
        "top_below_index_20d": _rank("vs_index_20d_pct", False),
    }
