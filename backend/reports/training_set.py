"""歷史『回放選股規則』訓練集產生器（方案 B）。

edge 模型的訓練樣本＝「系統選了某檔（當天特徵）→ 後來漲/跌（標籤）」的配對。線上只靠每日
晨報真的選過的標的累積，要好幾週才湊夠。這裡改成**在歷史上回放選股規則**：對 parquet 裡
每一個交易日，用與線上完全相同的 movers 排行（top_gainers/foreign_buy=偏多，losers/
foreign_sell/short_ratio/below_index=偏空）選出「模擬歷史選股」，配上**當天的 point-in-time
特徵**與**往後 5/20 日的真實漲跌標籤**，瞬間產出數千筆與線上同分布的樣本。

防未來洩漏（鐵律）：
- 特徵與選股排行只用 trade_date ≤ D 的資料（用 shift/rolling 自然滿足）。
- 標籤只用 trade_date > D 的未來收盤（close.shift(-h)）。
- 訓練端再做時間序 walk-forward 切分（見 strategy_calibration）。

純讀本機 parquet、CPU 運算，無外部 API、零 LLM 成本。對齊 tw_features 的 movers/特徵公式
（MIN_AMOUNT_TWD 流動性門檻、排除 ETF、top=8）以維持與線上選股同分布。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import universe
from config import settings
from reports.backtest import FEATURE_COLUMNS, featurize, horizons
from storage import local_store

logger = logging.getLogger("ai-market-backend.training_set")

TW_MARKET = "tw"
INDEX_SYMBOL = "TWII"
MIN_AMOUNT_TWD = 50_000_000                       # 對齊 tw_features 流動性門檻
_ETF_SECTORS = {"ETF", "上櫃指數股票型基金(ETF)", "受益證券"}
_TOP = 8                                          # 對齊 tw_features._movers top=8
PARQUET_ROOT = Path(settings.local_storage_path) / "local_parquet"
TRAINING_SET_PATH = Path(settings.local_storage_path) / "strategy" / "training_set.parquet"
_MIN_ROWS = 25                                    # 至少要能算出 return_20d


def _signed_streak(values: list[float]) -> list[int]:
    """每個位置：到該日為止連續同向（買超正/賣超負）天數；0 或缺值歸 0。對齊 _net_streak 語意。"""
    out: list[int] = []
    cur = 0
    sign = 0
    for v in values:
        if v is None or (isinstance(v, float) and np.isnan(v)) or v == 0:
            cur, sign = 0, 0
        else:
            s = 1 if v > 0 else -1
            cur = cur + s if s == sign else s
            sign = s
        out.append(cur)
    return out


def _index_ret20() -> pd.Series:
    """大盤 20 日報酬（%），index＝trade_date。供算個股相對大盤強弱 vs_index_20d_pct。"""
    idx = local_store.read_prices(INDEX_SYMBOL, TW_MARKET)
    if idx.empty:
        return pd.Series(dtype=float)
    idx = idx.copy()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"])
    idx = idx.sort_values("trade_date")
    close = idx["close"].astype(float)
    ret20 = (close / close.shift(20) - 1) * 100
    return pd.Series(ret20.values, index=idx["trade_date"].values)


def _symbol_long(sym: str, hs: list[int]) -> pd.DataFrame | None:
    """單檔 → 每日 point-in-time 特徵 + 前瞻標籤的長表（含排行所需指標）。資料太短回 None。"""
    px = local_store.read_prices(sym, TW_MARKET)
    if px.empty or len(px) < _MIN_ROWS:
        return None
    px = px.copy()
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    px = px.sort_values("trade_date").reset_index(drop=True)
    close = px["close"].astype(float)
    ret1 = close.pct_change()

    f = pd.DataFrame({"trade_date": px["trade_date"], "symbol": sym})
    f["sector"] = universe.sector_of(sym)
    f["amount"] = px["amount"].astype(float) if "amount" in px.columns else np.nan
    f["return_1d_pct"] = (ret1 * 100).round(2)
    f["return_5d_pct"] = (close / close.shift(5) - 1) * 100
    f["return_20d_pct"] = (close / close.shift(20) - 1) * 100
    f["volatility_20d_pct"] = ret1.rolling(20).std() * 100
    f["above_ma20"] = (close > close.rolling(20).mean()).astype(float)
    f["above_ma60"] = (close > close.rolling(60).mean()).astype(float)
    for h in hs:                                  # 前瞻標籤用 shift(-h)（嚴格未來，無洩漏）
        f[f"fwd_return_{h}"] = (close.shift(-h) / close - 1) * 100

    # 籌碼：foreign/trust 連續買賣超 + 外資 5 日合計（張）
    chip = local_store.read_chip(sym, TW_MARKET)
    if not chip.empty:
        chip = chip.copy()
        chip["trade_date"] = pd.to_datetime(chip["trade_date"])
        chip = chip.sort_values("trade_date")
        m = px[["trade_date"]].merge(chip, on="trade_date", how="left")
        foreign = m["foreign_net_buy"].astype(float)
        f["foreign_net_buy_5d_lots"] = (foreign.rolling(5).sum() / 1000).round()
        f["foreign_net_streak"] = _signed_streak(foreign.tolist())
        f["trust_net_streak"] = _signed_streak(m["trust_net_buy"].astype(float).tolist())
    else:
        f["foreign_net_buy_5d_lots"] = np.nan
        f["foreign_net_streak"] = np.nan
        f["trust_net_streak"] = np.nan

    # 融資券：融資 5 日增減（張）+ 資券比（融券/融資%）
    margin = local_store.read_margin(sym, TW_MARKET)
    if not margin.empty:
        margin = margin.copy()
        margin["trade_date"] = pd.to_datetime(margin["trade_date"])
        margin = margin.sort_values("trade_date")
        m = px[["trade_date"]].merge(margin, on="trade_date", how="left")
        mb = m["margin_balance"].astype(float)
        sb = m["short_balance"].astype(float)
        f["margin_chg_5d_lots"] = ((mb - mb.shift(5)) / 1000).round()
        f["short_margin_ratio_pct"] = np.where(mb > 0, (sb / mb * 100).round(2), np.nan)
    else:
        f["margin_chg_5d_lots"] = np.nan
        f["short_margin_ratio_pct"] = np.nan

    return f


def _emit_samples(day_df: pd.DataFrame, hs: list[int]) -> list[dict[str, Any]]:
    """單一交易日的橫斷面 → 回放選股規則 → 模擬選股樣本（含 5/20 日標籤）。"""
    liquid = day_df[(day_df["amount"].fillna(0) >= MIN_AMOUNT_TWD)
                    & (~day_df["sector"].isin(_ETF_SECTORS))]
    if liquid.empty:
        return []

    def _top(col: str, largest: bool) -> set[str]:
        s = liquid[liquid[col].notna()]
        if s.empty:
            return set()
        s = s.nlargest(_TOP, col) if largest else s.nsmallest(_TOP, col)
        return set(s["symbol"])

    # 對齊線上：偏多＝漲幅榜∪外資買超榜；偏空＝跌幅榜∪外資賣超榜∪資券比榜∪弱於大盤榜
    bull = _top("return_5d_pct", True) | _top("foreign_net_buy_5d_lots", True)
    bear = (_top("return_5d_pct", False) | _top("foreign_net_buy_5d_lots", False)
            | _top("short_margin_ratio_pct", True) | _top("vs_index_20d_pct", False))

    by_sym = {r["symbol"]: r for r in liquid.to_dict("records")}
    out: list[dict[str, Any]] = []
    for side, syms in (("watchlist", bull), ("caution", bear)):
        for sym in syms:
            row = by_sym.get(sym)
            if row is None:
                continue
            feat = featurize(row, side)
            for h in hs:
                fwd = row.get(f"fwd_return_{h}")
                if fwd is None or (isinstance(fwd, float) and np.isnan(fwd)):
                    continue                       # 該窗未到期 → 無標籤，略過
                label = 1 if (fwd > 0 if side == "watchlist" else fwd < 0) else 0
                sample = {
                    "as_of": pd.Timestamp(row["trade_date"]).date().isoformat(),
                    "symbol": sym, "side": side, "horizon": h,
                    "label": label, "fwd_return_pct": round(float(fwd), 2),
                }
                sample.update({c: feat.get(c) for c in FEATURE_COLUMNS})
                out.append(sample)
    return out


def build_training_set(hs: list[int] | None = None) -> dict[str, Any]:
    """掃全市場 parquet，回放選股規則產出歷史訓練集，落地 training_set.parquet。回統計。"""
    hs = hs or horizons()
    price_dir = PARQUET_ROOT / TW_MARKET
    if not price_dir.exists():
        return {"built": False, "reason": "no parquet"}
    t0 = time.monotonic()
    idx_ret20 = _index_ret20()

    frames: list[pd.DataFrame] = []
    syms = [p.stem for p in price_dir.glob("*.parquet") if p.stem != INDEX_SYMBOL]
    for sym in syms:
        try:
            fr = _symbol_long(sym, hs)
        except Exception as exc:  # noqa: BLE001 — 單檔壞資料不阻斷
            logger.debug("training_set 略過 %s: %s", sym, exc)
            continue
        if fr is not None:
            frames.append(fr)
    if not frames:
        return {"built": False, "reason": "no usable symbols"}

    big = pd.concat(frames, ignore_index=True)
    big["vs_index_20d_pct"] = (big["return_20d_pct"]
                               - big["trade_date"].map(idx_ret20)).round(2)

    samples: list[dict[str, Any]] = []
    for _d, day_df in big.groupby("trade_date"):
        samples.extend(_emit_samples(day_df, hs))

    if not samples:
        return {"built": False, "reason": "no samples (forward labels not matured yet)"}
    ds = pd.DataFrame(samples)
    TRAINING_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    ds.to_parquet(TRAINING_SET_PATH, engine="pyarrow", index=False)

    per_h = {int(h): int((ds["horizon"] == h).sum()) for h in hs}
    stats = {
        "built": True,
        "rows": len(ds),
        "symbols_scanned": len(syms),
        "symbols_used": len(frames),
        "distinct_days": int(big["trade_date"].nunique()),
        "date_range": [ds["as_of"].min(), ds["as_of"].max()],
        "samples_per_horizon": per_h,
        "elapsed_sec": round(time.monotonic() - t0, 1),
    }
    logger.info("歷史訓練集建好：%d 列、%d 檔、%d 天、各窗 %s（%.1fs）",
                stats["rows"], stats["symbols_used"], stats["distinct_days"], per_h, stats["elapsed_sec"])
    return stats


def load_training_set(h: int) -> pd.DataFrame:
    """讀回某窗的歷史樣本（無檔回空）。"""
    if not TRAINING_SET_PATH.exists():
        return pd.DataFrame()
    df = pd.read_parquet(TRAINING_SET_PATH)
    return df[df["horizon"] == h].reset_index(drop=True)


def build_if_stale(max_age_days: float = 3.0) -> dict[str, Any]:
    """訓練集不存在或過舊（>max_age_days）才重建——讓每日晨報迴圈便宜地保持新鮮。"""
    if TRAINING_SET_PATH.exists():
        age_days = (time.time() - TRAINING_SET_PATH.stat().st_mtime) / 86400
        if age_days <= max_age_days:
            return {"built": False, "reason": f"fresh ({age_days:.1f}d)"}
    return build_training_set()
