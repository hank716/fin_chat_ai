"""市場恐慌/避險 regime（TAIFEX 選擇權 Put/Call Ratio；VIX 若日後可取再加）。

市場級日序列（同日對所有股同值）：對橫斷面 risk/meta 標籤近乎無效，主要供 **市場 regime / 總曝險**。
- `pc_feature_frame()`：日期→市場特徵（pc_oi_ratio / pc_oi_z20 / pc_vol_ratio / pc_oi_chg5），訓練端 map-by-date。
- `latest_pc_features()`：最新一日特徵，serve 端注入候選（與訓練定義一致）。
- `market_fear_score(asof)`：透明分位 gauge——P/C-OI z-score 在全史的百分位（高＝避險/恐慌濃、未來市場風險偏高）。
  無擬合、可解讀（市場時序樣本小，刻意不在小樣本硬擬 ML）。
"""
from __future__ import annotations

import logging

import pandas as pd

from data_sources import taifex_loader

logger = logging.getLogger("ai-market-backend.market_regime")

# 市場級特徵（與 mkt_trend/mkt_vol 同類，加進 FEATURE_COLUMNS 量測；主要供 regime gauge）。
PC_FEATURES = ["pc_oi_ratio", "pc_oi_z20", "pc_vol_ratio", "pc_oi_chg5"]


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """加衍生欄：OI 比的 20 日 z-score 與 5 日變化（恐慌主訊號＝put 避險相對強度的異常）。"""
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)
    oi = df["pc_oi_ratio"].astype(float)
    mean20 = oi.rolling(20, min_periods=10).mean()
    std20 = oi.rolling(20, min_periods=10).std()
    df["pc_oi_z20"] = ((oi - mean20) / std20).where(std20 > 0)
    df["pc_oi_chg5"] = oi - oi.shift(5)
    return df


def pc_feature_frame() -> pd.DataFrame:
    """回 DataFrame[trade_date + PC_FEATURES]（供 training_set._build_big merge）；無資料回空。"""
    raw = taifex_loader.read_pcr()
    if raw.empty:
        return pd.DataFrame(columns=["trade_date", *PC_FEATURES])
    df = _enrich(raw)
    return df[["trade_date", *PC_FEATURES]]


def latest_pc_features() -> dict[str, float]:
    """serve 端：最新一日的 P/C 市場特徵（缺值略過）；無資料回空 dict。"""
    df = pc_feature_frame()
    if df.empty:
        return {}
    last = df.dropna(subset=["pc_oi_ratio"]).tail(1)
    if last.empty:
        return {}
    row = last.iloc[0]
    return {f: round(float(row[f]), 4) for f in PC_FEATURES if pd.notna(row[f])}


def market_fear_score(asof: str | None = None) -> float | None:
    """透明恐慌分位 gauge ∈ [0,1]：P/C-OI z-score 在全史的百分位（高＝避險/恐慌濃）。

    asof（YYYY-MM-DD）截到該日為止算百分位（回測逐日用）；None＝用最新日。資料不足回 None。
    """
    df = pc_feature_frame()
    if df.empty:
        return None
    z = df.dropna(subset=["pc_oi_z20"]).copy()
    if asof is not None:
        z = z[z["trade_date"] <= pd.Timestamp(asof)]
    if len(z) < 30:
        return None
    cur = float(z["pc_oi_z20"].iloc[-1])
    pct = float((z["pc_oi_z20"] <= cur).mean())        # 當前 z 在歷史的百分位
    return round(pct, 4)
