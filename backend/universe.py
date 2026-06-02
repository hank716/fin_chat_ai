"""台股 watchlist / 族群設定載入（M2-report step A）。

讀 configs/universe/tw.json（掛載進容器 /app/configs），給回補、features、報告共用：
  - watchlist_symbols(): 要回補/分析的台股代號集合（不含大盤指數）
  - sector_of(symbol) / sectors(): 族群對應
  - display_name(symbol): 中文名（報告顯示用）
  - index_meta(): 大盤指數 (TWII / ^TWII) 設定
  - us_to_tw_linkage(): 美股族群 → 台股族群的『可能影響』對應（跨市場敘事）

純 stdlib json，零新依賴。檔案缺失或壞掉時回空集合（不阻斷管線）。
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("ai-market-backend.universe")

# backend code 在 /app，configs 掛在 /app/configs
CONFIG_PATH = Path(__file__).resolve().parent / "configs" / "universe" / "tw.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — 設定缺失不該炸掉整條管線
        logger.warning("台股 universe 設定讀取失敗 (%s): %s", CONFIG_PATH, exc)
        return {}


def sectors() -> dict[str, list[str]]:
    """{族群名: [代號,...]}。"""
    return dict(_load().get("sectors", {}))


@lru_cache(maxsize=1)
def watchlist_symbols() -> frozenset[str]:
    """所有要回補/分析的台股代號（攤平族群，去重，不含大盤指數）。"""
    out: set[str] = set()
    for syms in _load().get("sectors", {}).values():
        out.update(syms)
    return frozenset(out)


@lru_cache(maxsize=1)
def _symbol_to_sector() -> dict[str, str]:
    m: dict[str, str] = {}
    for sector, syms in _load().get("sectors", {}).items():
        for s in syms:
            m.setdefault(s, sector)
    return m


def sector_of(symbol: str) -> str | None:
    return _symbol_to_sector().get(symbol)


def display_name(symbol: str) -> str:
    """中文名，找不到回代號本身。"""
    return _load().get("names", {}).get(symbol, symbol)


def index_meta() -> dict[str, str]:
    """大盤指數設定，預設 TWII / ^TWII。"""
    return _load().get("index", {"symbol": "TWII", "yf": "^TWII", "name": "加權指數"})


def us_to_tw_linkage() -> dict[str, list[str]]:
    raw = _load().get("us_to_tw_linkage", {})
    return {k: v for k, v in raw.items() if not k.startswith("_")}
