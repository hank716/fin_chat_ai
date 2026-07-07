"""Qlib 離線 image 共用常數/路徑/小工具（自足，不 import backend）。

只讀掛載的 /data（= repo storage/）與 /app/configs（= repo configs/），寫 JSON 回 /data/strategy。
與 backend 對齊但刻意不共用程式碼——保持隔離 image 零 backend 依賴。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("qlib-offline")

# ── 路徑（對齊 backend local_store / training_set 慣例）──
STORAGE = Path(os.environ.get("LOCAL_STORAGE_PATH", "/data"))
PARQUET_TW = STORAGE / "local_parquet" / "tw"
ADJ_FACTORS_PATH = STORAGE / "local_parquet" / "tw_adj_factors.parquet"   # 除權息因子表（spec 017）
QLIB_DATA = STORAGE / "qlib_data"            # Qlib provider_uri（dump 落地處）
STRATEGY_DIR = STORAGE / "strategy"
EVAL_PATH = STRATEGY_DIR / "qlib_eval.json"
META_PATH = STRATEGY_DIR / "qlib_meta.json"
SCORES_DIR = STRATEGY_DIR / "qlib_scores"

# universe 快取：backend 容器把 ./configs 掛到 /app/configs；本 image 同樣掛載。
CONFIGS = Path(os.environ.get("CONFIGS_DIR", "/app/configs"))
UNIVERSE_CACHE = CONFIGS / "universe" / "tw_all.json"

# ── 對齊 training_set 的選股規則常數 ──
INDEX_SYMBOL = "TWII"
MIN_AMOUNT_TWD = 50_000_000                   # 主池流動性門檻（對齊 training_set.MIN_AMOUNT_TWD）
# ETF 類股（對齊 training_set._ETF_SECTORS，另含 universe 實際出現的「上櫃ETF」）；
# 排除原因：ETF 低波動易判、會灌水方向/風險訊號（見記憶 drawdown-target-breakthrough）。
_ETF_KEYWORDS = ("ETF", "受益證券", "指數股票型基金", "受益憑證")


def horizons() -> list[int]:
    """評估時間窗（對齊 backend config.backtest_horizons，預設 5,20）。"""
    raw = os.environ.get("BACKTEST_HORIZONS", "5,20")
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return out or [5, 20]


def _is_etf(sector: str | None) -> bool:
    s = sector or ""
    return any(k in s for k in _ETF_KEYWORDS)


def load_universe_symbols() -> list[str]:
    """讀 tw_all.json → 個股代號清單（排除 ETF 與 TWII）；缺檔回空清單。

    與 training_set 一致：universe 來自 FinMind TaiwanStockInfo 快照（universe.py 落地），
    sector 用 industry_category。排除 ETF/受益證券。
    """
    if not UNIVERSE_CACHE.exists():
        log.warning("universe 快取不存在: %s（將退回掃 parquet 全市場）", UNIVERSE_CACHE)
        return []
    try:
        data = json.loads(UNIVERSE_CACHE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("universe 快取讀取失敗: %s", exc)
        return []
    syms = data.get("symbols", {}) or {}
    out = [sid for sid, meta in syms.items()
           if sid != INDEX_SYMBOL and not _is_etf((meta or {}).get("sector"))]
    log.info("universe：%d 檔個股（已排除 ETF/TWII）", len(out))
    return out


def available_symbols() -> list[str]:
    """實際要餵 Qlib 的代號＝universe ∩ 有 parquet 的檔；universe 缺檔則退回掃全 parquet。"""
    have = {p.stem for p in PARQUET_TW.glob("*.parquet")} if PARQUET_TW.exists() else set()
    have.discard(INDEX_SYMBOL)
    uni = set(load_universe_symbols())
    syms = sorted(have & uni) if uni else sorted(have)
    log.info("可用代號：%d 檔（parquet ∩ universe）", len(syms))
    return syms


def load_adj_index() -> dict:
    """讀除權息因子表 → {sym: (ex_dates np.datetime64 排序, cumprod, total)}；缺檔回 {}。

    供 dump 做讀取端還原（spec 017 / WP2.1）：adj[t]=raw[t]×∏{ex_date>t}factor。隔離 image
    刻意複製此邏輯（不 import backend），與 backend local_store._apply_adjustment 同定義。
    """
    import numpy as np
    import pandas as pd

    if not ADJ_FACTORS_PATH.exists():
        log.warning("除權息因子表不存在: %s（qlib dump 退回原始價）", ADJ_FACTORS_PATH)
        return {}
    df = pd.read_parquet(ADJ_FACTORS_PATH)
    if df.empty:
        return {}
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    out: dict = {}
    for sym, g in df.sort_values("ex_date").groupby("symbol"):
        cum = np.cumprod(g["adj_factor"].astype(float).to_numpy())
        out[str(sym)] = (g["ex_date"].to_numpy(), cum, float(cum[-1]) if len(cum) else 1.0)
    log.info("除權息因子表：%d 檔有股利（qlib dump 用還原價）", len(out))
    return out


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
