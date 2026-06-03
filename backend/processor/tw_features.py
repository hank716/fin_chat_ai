"""台股 features（M2-report step C）。

讀回補落地的台股價格 + 籌碼 parquet，算出給 AI 敘事/選股用的結構化數字：
  - index：大盤加權指數技術面
  - stocks：個股技術面（MA/報酬/波動）+ 相對大盤強弱 + 籌碼（三大法人淨買賣超、連續買超天數）
  - sectors：族群聚合（平均報酬、外資合計買超、領漲標的）
  - movers：漲跌幅 / 外資買賣超排行（方便 AI 點出候選觀察標的）

只算數字、不做 AI 判讀。NaN → None。籌碼單位輸出『張』（股/1000）便於閱讀。
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

import universe
from config import settings
from storage import local_store

logger = logging.getLogger("ai-market-backend.tw_features")

TW_MARKET = "tw"
INDEX_SYMBOL = "TWII"
PRICE_DIR = Path(settings.local_storage_path) / "local_parquet" / "tw"
# movers 流動性門檻：最新成交金額（元）。濾掉低量小型股的極端漲跌雜訊。
MIN_AMOUNT_TWD = 50_000_000


def _available_symbols() -> set[str]:
    """storage 中實際有價格 parquet 的台股代號。"""
    if not PRICE_DIR.exists():
        return set()
    return {p.stem for p in PRICE_DIR.glob("*.parquet")}


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
        "_amount": _clean(float(last["amount"])) if "amount" in df.columns
        and not pd.isna(last.get("amount")) else None,  # 最新成交金額(元)，供流動性過濾
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

    # 全市場計算（迭代實際有 parquet 的 universe 標的）
    all_stocks: dict[str, Any] = {}
    as_of_dates: list[str] = []
    for sym in _available_symbols():
        if sym == INDEX_SYMBOL:
            continue
        price = _price_block(local_store.read_prices(sym, TW_MARKET))
        if price is None:
            continue
        ret20 = price.pop("_ret20_raw", None)
        vs_index = (
            round(ret20 - index_ret20, 2)
            if ret20 is not None and index_ret20 is not None else None
        )
        all_stocks[sym] = {
            "name": universe.display_name(sym),
            "sector": universe.sector_of(sym),
            **price,
            "vs_index_20d_pct": vs_index,
            **_chip_block(local_store.read_chip(sym, TW_MARKET)),
            **_margin_block(local_store.read_margin(sym, TW_MARKET)),
        }
        if price.get("as_of"):
            as_of_dates.append(price["as_of"])

    sectors = _aggregate_sectors(all_stocks)
    movers = _movers(all_stocks)
    # 只把「movers 出現過的標的」的明細丟給 AI（全市場 2000+ 檔不能全塞進 prompt）
    focus = {it["symbol"] for lst in movers.values() for it in lst}
    # 丟給 AI 前移除內部用的底線欄位（_amount 等）；並補基本面（月營收 YoY/MoM）
    from data_sources.finmind_loader import FinMindBackoff
    from processor.fundamentals import build_fundamentals
    stocks = {}
    for s in focus:
        if s not in all_stocks:
            continue
        entry = {k: v for k, v in all_stocks[s].items() if not k.startswith("_")}
        try:
            fu = build_fundamentals(s)
        except FinMindBackoff as exc:
            # FinMind 額度耗盡/封鎖：晨報照出，只是這批 focus 缺基本面，不為此中斷整份報告。
            logger.warning("基本面抓取因 FinMind 退避中止，本批 focus 略過基本面: %s", exc)
            stocks[s] = entry
            break
        if fu:
            entry["fundamentals"] = fu
        stocks[s] = entry

    as_of = max(as_of_dates) if as_of_dates else (index_block or {}).get("as_of")
    return {
        "as_of": as_of,
        "window": window,
        "universe_size": len(all_stocks),
        "index": index_block,
        "stocks": stocks,
        "sectors": sectors,
        "movers": movers,
        "notes": "stocks 僅含 movers 涉及之標的明細（全市場共 universe_size 檔，完整資料在系統內）；"
        "籌碼單位張(=股/1000)；vs_index_20d_pct=個股20日報酬減大盤(相對強弱)；net_streak 正=連買天數負=連賣。",
    }


def _aggregate_sectors(stocks: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sector, syms in universe.sectors().items():
        members = [(s, stocks[s]) for s in syms if s in stocks]
        if len(members) < 3:  # 太少成員的產業別不聚合（降雜訊）
            continue
        ret20s = [e["return_20d_pct"] for _, e in members if e.get("return_20d_pct") is not None]
        ret5s = [e["return_5d_pct"] for _, e in members if e.get("return_5d_pct") is not None]
        foreign5 = [
            e["foreign_net_buy_5d_lots"] for _, e in members
            if e.get("foreign_net_buy_5d_lots") is not None
        ]
        # 領漲股只從有流動性的成員挑
        leaders = sorted(
            (m for m in members
             if m[1].get("return_20d_pct") is not None
             and (m[1].get("_amount") or 0) >= MIN_AMOUNT_TWD),
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


_ETF_SECTORS = {"ETF", "上櫃指數股票型基金(ETF)", "受益證券"}


def _movers(stocks: dict[str, Any], top: int = 8) -> dict[str, Any]:
    # 只在「有流動性、且非 ETF」的個股中排行（ETF 法人申贖流量會蓋過選股訊號）
    liquid = {
        s: e for s, e in stocks.items()
        if (e.get("_amount") or 0) >= MIN_AMOUNT_TWD and e.get("sector") not in _ETF_SECTORS
    }

    def _rank(key: str, reverse: bool) -> list[dict[str, Any]]:
        items = [(s, e) for s, e in liquid.items() if e.get(key) is not None]
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


# ── 即時查單一標的（清單外，給 Discord /ask 用）─────────────────────────

@lru_cache(maxsize=1)
def _finmind_names() -> dict[str, str]:
    """{stock_id: stock_name} 快取（給清單外標的顯示中文名）。"""
    try:
        from data_sources import finmind_loader
        return {r["stock_id"]: r["stock_name"] for r in finmind_loader.get_taiwan_stock_info()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("FinMind 股名清單取得失敗: %s", exc)
        return {}


def _is_stale(df: pd.DataFrame, max_age_days: int = 4) -> bool:
    if df.empty:
        return True
    last = pd.to_datetime(df["trade_date"]).max().date()
    return (date.today() - last).days > max_age_days


def _fetch_and_land(symbol: str) -> None:
    """即時抓單檔近 120 日價/籌/資券並落 parquet（idempotent upsert，順便快取）。"""
    from data_sources import finmind_loader
    from data_sources.ingest import _dq_filter

    end = date.today()
    s, e = (end - timedelta(days=120)).isoformat(), end.isoformat()
    for fetch, write in (
        (finmind_loader.fetch_stock_prices_normalized, local_store.write_prices),
        (finmind_loader.fetch_stock_chip_normalized, local_store.write_chip),
        (finmind_loader.fetch_stock_margin_normalized, local_store.write_margin),
    ):
        try:
            rows = _dq_filter(fetch(symbol, s, e))
            if rows:
                write(rows, TW_MARKET)
        except Exception as exc:  # noqa: BLE001 — 單一資料類失敗不阻斷
            logger.warning("即時抓 %s (%s) 失敗: %s", symbol, fetch.__name__, exc)


def build_adhoc_symbol(symbol: str) -> dict[str, Any] | None:
    """即時查清單外台股的技術面+籌碼+資券（不在每日 universe）。查無資料回 None。"""
    df = local_store.read_prices(symbol, TW_MARKET)
    if _is_stale(df):
        _fetch_and_land(symbol)
        df = local_store.read_prices(symbol, TW_MARKET)
    price = _price_block(df)
    if price is None:
        return None
    ret20 = price.pop("_ret20_raw", None)
    index_block = _price_block(local_store.read_prices(INDEX_SYMBOL, TW_MARKET))
    index_ret20 = index_block.get("_ret20_raw") if index_block else None
    vs_index = (
        round(ret20 - index_ret20, 2)
        if ret20 is not None and index_ret20 is not None else None
    )
    return {
        "name": _finmind_names().get(symbol, symbol),
        "sector": "查詢標的（非每日追蹤清單）",
        **price,
        "vs_index_20d_pct": vs_index,
        **_chip_block(local_store.read_chip(symbol, TW_MARKET)),
        **_margin_block(local_store.read_margin(symbol, TW_MARKET)),
    }
