"""共用測試夾具（防洩漏測試套件的基座）。

所有測試一律用**合成的記憶體內** price/chip/margin/fundamentals frame，並 monkeypatch
storage.local_store 的讀取函式——不碰真實 parquet、不打任何外部 API，純確定性運算。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from storage.local_store import CHIP_COLUMNS, MARGIN_COLUMNS, PRICE_COLUMNS


def make_price_df(closes, *, highs=None, lows=None, opens=None, amounts=None,
                  start="2024-01-02"):
    """由 close 序列組出 PRICE_COLUMNS 形狀的日 OHLCV（交易日用 business day 近似）。"""
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    highs = np.asarray(highs, dtype=float) if highs is not None else closes.copy()
    lows = np.asarray(lows, dtype=float) if lows is not None else closes.copy()
    opens = np.asarray(opens, dtype=float) if opens is not None else closes.copy()
    amounts = np.asarray(amounts, dtype=float) if amounts is not None else np.full(n, 1e8)
    dates = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame({
        "trade_date": dates,
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.where(closes > 0, amounts / closes, 0.0),
        "amount": amounts, "source": "test",
    })[PRICE_COLUMNS]


def empty_chip() -> pd.DataFrame:
    return pd.DataFrame(columns=CHIP_COLUMNS)


def empty_margin() -> pd.DataFrame:
    return pd.DataFrame(columns=MARGIN_COLUMNS)


@pytest.fixture
def price_factory():
    """回傳 make_price_df，讓測試自行造合成行情。"""
    return make_price_df


@pytest.fixture
def install_symbol(monkeypatch):
    """安裝合成資料給 training_set._symbol_long / backtest 讀取路徑。

    回傳 install(sym, price_df, *, chip=None, margin=None, rev=None, fin=None,
    sector="TestSector")；可多次呼叫註冊多檔。所有 local_store 讀取都被導向記憶體 dict。
    """
    import universe
    from processor import fundamentals_history
    from storage import local_store

    prices: dict[str, pd.DataFrame] = {}
    chips: dict[str, pd.DataFrame] = {}
    margins: dict[str, pd.DataFrame] = {}
    funds: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    sectors: dict[str, str] = {}

    def fake_read_prices(symbol, market, *, adjusted=False):
        # 合成測試無除權息因子；adjusted 與否回同一 frame（訓練端 adjusted=True 走此路徑）
        return prices.get(symbol, pd.DataFrame(columns=PRICE_COLUMNS)).copy()

    def fake_read_chip(symbol, market):
        return chips.get(symbol, pd.DataFrame(columns=CHIP_COLUMNS)).copy()

    def fake_read_margin(symbol, market):
        return margins.get(symbol, pd.DataFrame(columns=MARGIN_COLUMNS)).copy()

    def fake_read_history(symbol):
        return funds.get(symbol, (pd.DataFrame(), pd.DataFrame()))

    def fake_sector_of(symbol):
        return sectors.get(symbol, "TestSector")

    # local_store 是同一個模組物件（training_set / backtest 皆 `from storage import local_store`），
    # 因此在模組上打補丁即對兩處生效。
    monkeypatch.setattr(local_store, "read_prices", fake_read_prices)
    monkeypatch.setattr(local_store, "read_chip", fake_read_chip)
    monkeypatch.setattr(local_store, "read_margin", fake_read_margin)
    monkeypatch.setattr(fundamentals_history, "read_history", fake_read_history)
    monkeypatch.setattr(universe, "sector_of", fake_sector_of)

    def install(sym, price_df, *, chip=None, margin=None, rev=None, fin=None,
                sector="TestSector"):
        prices[sym] = price_df
        if chip is not None:
            chips[sym] = chip
        if margin is not None:
            margins[sym] = margin
        funds[sym] = (rev if rev is not None else pd.DataFrame(),
                      fin if fin is not None else pd.DataFrame())
        sectors[sym] = sector

    return install
