"""spec 018 Phase 4：台股指數不收週末列（yfinance 偶爾給出非交易日的 ^TWII）。"""
from __future__ import annotations

import pandas as pd

from data_sources import yfinance_loader as yl


def _history(days):
    idx = pd.DatetimeIndex(pd.to_datetime(days))
    return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 0}, index=idx)


def test_tw_index_drops_weekend_rows_crypto_keeps_them(monkeypatch):
    days = ["2026-09-18", "2026-09-20", "2026-09-21"]          # 9/20 週日
    monkeypatch.setattr(yl.rate_limiter, "acquire", lambda *_: None)
    monkeypatch.setattr(yl.yf, "Ticker", lambda _sym: type("T", (), {"history": lambda self, **_: _history(days)})())
    monkeypatch.setattr(yl, "INTERMARKET_SYMBOLS", [("^TWII", "TWII", "tw"), ("BTC-USD", "BTC", "crypto")])
    got = yl.fetch_intermarket()
    assert [r.trade_date.isoformat() for r in got["tw"]] == ["2026-09-18", "2026-09-21"]
    assert len(got["crypto"]) == 3
