"""台股 universe / 族群（M8：全市場，動態由 FinMind 建）。

改為涵蓋**全台股**（上市 twse + 上櫃 tpex，含 ETF；不含興櫃 emerging），
來源 = FinMind TaiwanStockInfo（stock_id / stock_name / industry_category / type）。
族群直接用 industry_category（57 類真實產業別）。

快取到 configs/universe/tw_all.json（掛載目錄，跨重啟保留；缺檔自動重建）。
跨市場 linkage（美股 → 台股產業）與大盤指數設定寫死於此。
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("ai-market-backend.universe")

CONFIG_DIR = Path(__file__).resolve().parent / "configs" / "universe"
CACHE_PATH = CONFIG_DIR / "tw_all.json"

INDEX_META = {"symbol": "TWII", "yf": "^TWII", "name": "加權指數"}
INCLUDE_TYPES = {"twse", "tpex"}  # 不含 emerging（興櫃，流動性低、無全市場日端點）

# 美股/跨市場 → 台股產業別的「可能影響」對應（用 industry_category 名稱）
US_TO_TW_LINKAGE = {
    "SOX": ["半導體業", "電腦及週邊設備業", "電子零組件業", "光電業"],
    "NASDAQ": ["半導體業", "電腦及週邊設備業", "通信網路業", "其他電子業"],
    "SP500": ["金融保險業", "其他電子業"],
    "BTC": ["風險情緒"],
}


def build_universe_cache() -> dict[str, Any]:
    """從 FinMind 抓全清單並寫入快取檔。回傳 universe dict。"""
    from data_sources import finmind_loader

    info = finmind_loader.get_taiwan_stock_info()
    symbols: dict[str, Any] = {}
    for r in info:
        sid, t = r.get("stock_id"), r.get("type")
        if t not in INCLUDE_TYPES or not sid or sid in symbols:
            continue
        # FinMind TaiwanStockInfo 會夾帶產業/指數別的「非個股」列（stock_id 是
        # Semiconductor / TAIEX / TPEx 等英文字），台股真實代號一律數字開頭
        # （含 ETF 0050/00878、含字尾 00679B），用此過濾掉那些髒列。
        if not sid[:1].isdigit():
            continue
        symbols[sid] = {
            "name": r.get("stock_name") or sid,
            "sector": r.get("industry_category") or "其他",
            "market": t,
        }
    data = {"symbols": symbols, "index": INDEX_META, "linkage": US_TO_TW_LINKAGE}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    logger.info("台股 universe 快取重建：%d 檔", len(symbols))
    return data


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("universe 快取讀取失敗，改重建: %s", exc)
    try:
        return build_universe_cache()
    except Exception as exc:  # noqa: BLE001 — FinMind 不可用時不阻斷
        logger.warning("universe 建立失敗: %s", exc)
        return {"symbols": {}, "index": INDEX_META, "linkage": US_TO_TW_LINKAGE}


def _symbols() -> dict[str, Any]:
    return _load().get("symbols", {})


@lru_cache(maxsize=1)
def watchlist_symbols() -> frozenset[str]:
    """全台股代號（上市+上櫃，含 ETF）。"""
    return frozenset(_symbols().keys())


@lru_cache(maxsize=1)
def sectors() -> dict[str, list[str]]:
    """{產業別: [代號,...]}（由 industry_category 聚合）。"""
    out: dict[str, list[str]] = {}
    for sid, meta in _symbols().items():
        out.setdefault(meta.get("sector") or "其他", []).append(sid)
    return out


def sector_of(symbol: str) -> str | None:
    return (_symbols().get(symbol) or {}).get("sector")


def display_name(symbol: str) -> str:
    return (_symbols().get(symbol) or {}).get("name", symbol)


def market_of(symbol: str) -> str | None:
    return (_symbols().get(symbol) or {}).get("market")


def index_meta() -> dict[str, str]:
    return _load().get("index", INDEX_META)


def us_to_tw_linkage() -> dict[str, list[str]]:
    return _load().get("linkage", US_TO_TW_LINKAGE)
