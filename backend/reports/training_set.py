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
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import universe
from config import settings
from processor import fundamentals_history
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


def _triple_barrier(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                    vol20_pct: np.ndarray, h: int, band: float = 1.0) -> np.ndarray:
    """路徑感知 triple-barrier 首觸（side-agnostic，meta-labeling[5]）。

    對每個 t，未來 [t+1, t+h] 逐日掃首觸：上界＝`+band·σ_h`、下界＝`−band·σ_h`，σ_h＝日波動×√h
    （`vol20_pct` 是日報酬標準差×100）。回 {1 先觸上界, 0 兩界皆未觸, -1 先觸下界}；窗未滿留 NaN。
    對稱上下界＝正負號對多空兩側都成立（watchlist 成功＝+1、caution 成功＝-1，由 _emit_samples 取號）。
    同日同時觸 → 保守記下界優先（對齊 backtest.evaluate_item 的觸價優先序）。
    """
    n = len(close)
    out = np.full(n, np.nan)
    volh = vol20_pct * np.sqrt(h)
    for t in range(n - h):                        # t 需有完整未來窗 [t+1, t+h]
        v = volh[t]
        if not np.isfinite(v) or v <= 0 or close[t] <= 0:
            continue
        up, dn = band * v, -band * v
        res = 0
        for i in range(t + 1, t + h + 1):
            if (low[i] / close[t] - 1) * 100 <= dn:    # 下界優先（保守）
                res = -1
                break
            if (high[i] / close[t] - 1) * 100 >= up:
                res = 1
                break
        out[t] = res
    return out


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


def _index_trailing(max_date: pd.Timestamp | None = None) -> dict[str, pd.Series]:
    """大盤往回 5/20 日報酬（%），index＝trade_date，供個股相對強弱 vs_index_{5,20}d_pct。

    註：TWII 指數 parquet 目前僅近月（未隨個股回補 2 年），故此特徵在較舊日期多為 NaN；
    HistGBT 原生吃 NaN 不受影響，且標籤改用『同日橫斷面中位數』超額（見 _emit_samples），
    不再依賴 TWII 覆蓋度。

    max_date：資料截止日（含）。給定時只用 trade_date <= max_date 的列，讓 A/B 評估
    對照固定在同一份資料快照上（見 eval_snapshot 的 D1 歸因方法論）。
    """
    idx = local_store.read_prices(INDEX_SYMBOL, TW_MARKET, adjusted=True)
    if idx.empty:
        return {}
    idx = idx.copy()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"])
    idx = idx.sort_values("trade_date")
    if max_date is not None:
        idx = idx[idx["trade_date"] <= max_date]
    close = idx["close"].astype(float)
    idx_ret1 = close.pct_change()
    td = idx["trade_date"].values
    return {
        "trail_5": pd.Series(((close / close.shift(5) - 1) * 100).values, index=td),
        "trail_20": pd.Series(((close / close.shift(20) - 1) * 100).values, index=td),
        # regime[9]：大盤趨勢(20 日報酬)與波動狀態(20 日報酬標準差)，供模型學「行情依賴」的型態。
        "trend_20": pd.Series(((close / close.shift(20) - 1) * 100).values, index=td),
        "vol_20": pd.Series((idx_ret1.rolling(20).std() * 100).values, index=td),
    }


def current_market_regime() -> dict[str, float | None]:
    """serve 端用：以最新 TWII 算當前 regime 特徵，供晨報把它注入每檔候選（與訓練端定義一致）。"""
    idx = local_store.read_prices(INDEX_SYMBOL, TW_MARKET, adjusted=True)
    if idx.empty or len(idx) < 21:
        return {"mkt_trend_20d_pct": None, "mkt_vol_20d_pct": None}
    idx = idx.copy()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"])
    idx = idx.sort_values("trade_date")
    close = idx["close"].astype(float)
    trend = (close.iloc[-1] / close.iloc[-21] - 1) * 100
    vol = close.pct_change().tail(20).std() * 100
    out = {
        "mkt_trend_20d_pct": round(float(trend), 2) if pd.notna(trend) else None,
        "mkt_vol_20d_pct": round(float(vol), 2) if pd.notna(vol) else None,
    }
    try:                                              # 市場恐慌/避險特徵（最新一日；缺檔則略過）
        from processor import market_regime  # noqa: PLC0415
        out.update(market_regime.latest_pc_features())
    except Exception as exc:  # noqa: BLE001 — regime 取得失敗不阻斷打分
        logger.debug("latest_pc_features 失敗: %s", exc)
    return out


def _symbol_long(sym: str, hs: list[int], max_date: pd.Timestamp | None = None) -> pd.DataFrame | None:
    """單檔 → 每日 point-in-time 特徵 + 前瞻標籤的長表（含排行所需指標）。資料太短回 None。

    max_date：資料截止日（含）。給定時特徵與前瞻標籤都只由 trade_date <= max_date 的行情
    衍生（＝『假設今天是 max_date』的 point-in-time 快照），使評估可歸因、可重現（D1）。
    """
    px = local_store.read_prices(sym, TW_MARKET, adjusted=True)   # 除權息還原（spec 017）：報酬/波動/標籤跨除息日連續
    if px.empty or len(px) < _MIN_ROWS:
        return None
    px = px.copy()
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    px = px.sort_values("trade_date").reset_index(drop=True)
    if max_date is not None:
        px = px[px["trade_date"] <= max_date].reset_index(drop=True)
        if len(px) < _MIN_ROWS:
            return None
    close = px["close"].astype(float)
    low = px["low"].astype(float) if "low" in px.columns else close
    high = px["high"].astype(float) if "high" in px.columns else close
    ret1 = close.pct_change()

    amount = px["amount"].astype(float) if "amount" in px.columns else pd.Series(np.nan, index=px.index)
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()

    f = pd.DataFrame({"trade_date": px["trade_date"], "symbol": sym})
    f["sector"] = universe.sector_of(sym)
    f["amount"] = amount
    f["return_1d_pct"] = (ret1 * 100).round(2)
    f["return_5d_pct"] = (close / close.shift(5) - 1) * 100
    f["return_20d_pct"] = (close / close.shift(20) - 1) * 100
    f["volatility_20d_pct"] = ret1.rolling(20).std() * 100
    # 連續距離取代布林 above_maNN（與動能較不共線）；量能異常＝當日量/20 日均量。
    f["dist_ma20_pct"] = ((close / ma20 - 1) * 100).round(2)
    f["dist_ma60_pct"] = ((close / ma60 - 1) * 100).round(2)
    f["turnover_surge"] = (amount / amount.rolling(20).mean()).round(2)
    for h in hs:                                  # 前瞻標籤用 shift(-h)（嚴格未來，無洩漏）
        f[f"fwd_return_{h}"] = (close.shift(-h) / close - 1) * 100
        # 風險家族前瞻標籤（嚴格 t+1..t+h；reversed-rolling 取未來窗、min_periods=h 強制窗滿才有值＝
        # 與 fwd_return 到期語意一致，窗未滿留 NaN→該筆無標籤）。
        fwd_min_low = low[::-1].rolling(h, min_periods=h).min()[::-1].shift(-1)
        f[f"fwd_mae_{h}"] = (fwd_min_low / close - 1) * 100        # 最大回撤(MAE)：越負＝跌越深
        fwd_ret_std = ret1[::-1].rolling(h, min_periods=h).std()[::-1].shift(-1)
        f[f"fwd_vol_{h}"] = fwd_ret_std * 100                      # 實現波動度（未來日報酬標準差）
        fwd_abs_max = ret1.abs()[::-1].rolling(h, min_periods=h).max()[::-1].shift(-1)
        f[f"fwd_absmove_{h}"] = fwd_abs_max * 100                  # 未來最大單日絕對波幅
        # meta-labeling[5] 路徑感知 triple-barrier 首觸（watchlist 取向，caution 在 _emit_samples 反轉）。
        f[f"fwd_tb_{h}"] = _triple_barrier(
            close.to_numpy(), high.to_numpy(), low.to_numpy(),
            f["volatility_20d_pct"].to_numpy(), h)

    # 籌碼：foreign/trust/dealer 連續買賣超 + 外資/自營 5 日合計（張）
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
        if "dealer_net_buy" in m.columns:
            dealer = m["dealer_net_buy"].astype(float)
            f["dealer_net_buy_5d_lots"] = (dealer.rolling(5).sum() / 1000).round()
            f["dealer_net_streak"] = _signed_streak(dealer.tolist())
        else:
            f["dealer_net_buy_5d_lots"] = np.nan
            f["dealer_net_streak"] = np.nan
    else:
        f["foreign_net_buy_5d_lots"] = np.nan
        f["foreign_net_streak"] = np.nan
        f["trust_net_streak"] = np.nan
        f["dealer_net_buy_5d_lots"] = np.nan
        f["dealer_net_streak"] = np.nan

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

    # 基本面（point-in-time）：merge_asof(backward) 對每個 trade_date 取 known_date 已到的最新一期，
    # 杜絕未來洩漏（季報在截止日+緩衝後才看得到）。缺檔/缺期留 NaN（HistGBT 原生吃）。
    rev_df, fin_df = fundamentals_history.read_history(sym)
    days_since: list[pd.Series] = []                  # 事件窗用：距最近一次財報公開的天數
    for fdf, cols in ((rev_df, fundamentals_history.REV_FIELDS),
                      (fin_df, fundamentals_history.FIN_FIELDS)):
        if fdf.empty:
            for c in cols:
                f[c] = np.nan
            continue
        merged = pd.merge_asof(
            f[["trade_date"]].sort_values("trade_date"),
            fdf[["known_date", *cols]].sort_values("known_date"),
            left_on="trade_date", right_on="known_date", direction="backward",
        )
        for c in cols:
            f[c] = merged[c].to_numpy()
        kd = pd.to_datetime(merged["known_date"]).to_numpy()
        days_since.append((f["trade_date"].to_numpy() - kd) / np.timedelta64(1, "D"))
    # PEAD/事件窗維度（非特徵、供另池實驗切片）：距最近一次營收或財報公開的天數（取較近者）。
    if days_since:
        stacked = np.vstack(days_since)
        f["days_since_earnings"] = np.nanmin(stacked, axis=0)
    else:
        f["days_since_earnings"] = np.nan

    return f


def _emit_samples(day_df: pd.DataFrame, hs: list[int], *,
                  min_amount: float = MIN_AMOUNT_TWD, max_amount: float | None = None) -> list[dict[str, Any]]:
    """單一交易日的橫斷面 → 回放選股規則 → 模擬選股樣本（含 5/20 日**橫斷面超額**標籤）。

    標籤＝相對「同日全市場」的超額方向對不對（偏多正類＝贏過當日流動性股池中位數、偏空正類＝
    跌過中位數）。以橫斷面中位數當市場基準（而非 TWII）有兩個好處：剝除個股特徵無法預測的大盤
    beta、且不受 TWII 指數 parquet 覆蓋度限制（可涵蓋全部歷史日期）；也對齊 reranking 的真正
    目的（選贏過大盤的標的）。

    流動性帶 [min_amount, max_amount)：預設＝主池(>=50M、無上限)；改戰場實驗傳中小型帶(如 5M–50M)。
    """
    amt = day_df["amount"].fillna(0)
    mask = (amt >= min_amount) & (~day_df["sector"].isin(_ETF_SECTORS))
    if max_amount is not None:
        mask &= amt < max_amount
    liquid = day_df[mask]
    if liquid.empty:
        return []
    # 當日市場基準＝流動性股池各窗前瞻指標的中位數（先 dropna 避免對全 NaN 尾段算空集合）。
    def _median(prefix: str, h: int) -> float:
        col = liquid[f"{prefix}_{h}"].dropna()
        return float(col.median()) if not col.empty else float("nan")
    mkt_h = {h: _median("fwd_return", h) for h in hs}     # 方向超額基準
    mae_med = {h: _median("fwd_mae", h) for h in hs}      # 回撤風險基準（越負＝越深）
    vol_med = {h: _median("fwd_vol", h) for h in hs}      # 波動度基準
    abs_med = {h: _median("fwd_absmove", h) for h in hs}  # 大幅波動基準

    # 因子中性化殘差[6]：同日同產業(成員>=3)中位數當基準，剝掉產業/大盤共同成分，只留特異報酬；
    # 產業樣本不足退回市場中位數。殘差是比裸超額更乾淨的方向目標（rank 模型/殘差分類共用）。
    def _sector_med(h: int) -> dict[str, float]:
        g = liquid.dropna(subset=[f"fwd_return_{h}"]).groupby("sector")[f"fwd_return_{h}"]
        med, cnt = g.median(), g.count()
        return {s: float(med[s]) for s in med.index if cnt[s] >= 3}
    sec_med = {h: _sector_med(h) for h in hs}

    def _top(col: str, largest: bool) -> set[str]:
        s = liquid[liquid[col].notna()]
        if s.empty:
            return set()
        s = s.nlargest(_TOP, col) if largest else s.nsmallest(_TOP, col)
        return set(s["symbol"])

    # 對齊線上：偏多＝漲幅榜∪外資買超榜；偏空＝跌幅榜∪外資賣超榜∪資券比榜∪弱於大盤榜
    bull_lists = [_top("return_5d_pct", True), _top("foreign_net_buy_5d_lots", True)]
    bear_lists = [_top("return_5d_pct", False), _top("foreign_net_buy_5d_lots", False),
                  _top("short_margin_ratio_pct", True), _top("vs_index_20d_pct", False)]
    bull = set().union(*bull_lists)
    bear = set().union(*bear_lists)
    # conviction[5]：該檔同時命中幾個 component 選股清單（漲幅榜∩外資買超榜＝高把握）。
    def _conviction(sym: str, lists: list[set[str]]) -> int:
        return sum(1 for s in lists if sym in s)

    def _ok(v: Any) -> bool:
        return v is not None and not (isinstance(v, float) and np.isnan(v))

    def _median_label(val: Any, med: float, *, higher_is_pos: bool) -> tuple[float | None, int | None]:
        """與當日橫斷面中位數比：回 (round 值, 正類標籤)；缺值或無基準回 (None, None)。"""
        if not _ok(val) or not _ok(med):
            return None, None
        v = float(val)
        pos = (v > med) if higher_is_pos else (v < med)
        return round(v, 2), (1 if pos else 0)

    by_sym = {r["symbol"]: r for r in liquid.to_dict("records")}
    out: list[dict[str, Any]] = []
    for side, syms in (("watchlist", bull), ("caution", bear)):
        for sym in syms:
            row = by_sym.get(sym)
            if row is None:
                continue
            feat = featurize(row, side)
            conv = _conviction(sym, bull_lists if side == "watchlist" else bear_lists)
            for h in hs:
                fwd = row.get(f"fwd_return_{h}")
                if not _ok(fwd):
                    continue                       # 該窗未到期 → 無標籤，略過
                mkt = mkt_h.get(h)
                if not _ok(mkt):
                    continue                       # 當日無市場基準 → 算不出超額，略過
                excess = float(fwd) - float(mkt)
                label = 1 if (excess > 0 if side == "watchlist" else excess < 0) else 0
                # 因子中性化殘差[6]：產業中位數(不足退市場)當基準；resid_label=殘差方向、resid_return_pct=連續值(rank 模型用)。
                base = sec_med.get(h, {}).get(row.get("sector"))
                if not _ok(base):
                    base = float(mkt)
                resid = float(fwd) - float(base)
                resid_label = 1 if (resid > 0 if side == "watchlist" else resid < 0) else 0
                # 風險家族標籤（與 side 無關）：回撤越深/波動越大＝高風險正類。缺值留 None→訓練略過。
                fwd_mae_pct, risk_label = _median_label(row.get(f"fwd_mae_{h}"), mae_med.get(h), higher_is_pos=False)
                fwd_vol_pct, vol_label = _median_label(row.get(f"fwd_vol_{h}"), vol_med.get(h), higher_is_pos=True)
                fwd_absmove_pct, absmove_label = _median_label(row.get(f"fwd_absmove_{h}"), abs_med.get(h), higher_is_pos=True)
                # meta-labeling[5]：triple-barrier 首觸取號＝這筆「交易」是否成功（watchlist:+1 / caution:-1）。
                tb = row.get(f"fwd_tb_{h}")
                meta_label = (1 if (tb == 1 if side == "watchlist" else tb == -1) else 0) if _ok(tb) else None
                sample = {
                    "as_of": pd.Timestamp(row["trade_date"]).date().isoformat(),
                    "symbol": sym, "side": side, "horizon": h,
                    "label": label, "fwd_return_pct": round(float(fwd), 2),
                    "excess_vs_market_pct": round(excess, 2),
                    "resid_label": resid_label, "resid_return_pct": round(resid, 2),
                    "risk_label": risk_label, "fwd_mae_pct": fwd_mae_pct,
                    "vol_label": vol_label, "fwd_vol_pct": fwd_vol_pct,
                    "absmove_label": absmove_label, "fwd_absmove_pct": fwd_absmove_pct,
                    "meta_label": meta_label, "conviction": conv,    # meta-labeling[5]：成功標籤 + 訊號共振數
                    "days_since_earnings": (round(float(row["days_since_earnings"]), 1)
                                            if _ok(row.get("days_since_earnings")) else None),
                }
                sample.update({c: feat.get(c) for c in FEATURE_COLUMNS})
                out.append(sample)
    return out


def _overlap_weights(ds: pd.DataFrame) -> pd.Series:
    """平均唯一性近似權重[7]：同檔同窗、as_of 落在彼此前瞻窗內的樣本互相稀釋（weight=1/重疊數）。

    歷史回放每日 emit 的前瞻標籤在時間上重疊（h 日窗覆蓋 t..t+h），會讓 OOS 高估。對同
    (symbol, horizon) 計每筆 as_of 前後約 h 個交易日（以 1.5×h 日曆日近似）內的同群樣本數，
    取倒數當權重，傳進 model.fit(sample_weight=) 與評估，讓訓練/量測更誠實。
    """
    w = pd.Series(1.0, index=ds.index)
    aod = pd.to_datetime(ds["as_of"])
    for (_sym, h), idx in ds.groupby(["symbol", "horizon"]).groups.items():
        if len(idx) <= 1:
            continue
        days = np.sort(aod.loc[idx].values.astype("datetime64[D]").astype("int64"))
        order = aod.loc[idx].values.astype("datetime64[D]").astype("int64").argsort()
        span = int(round(int(h) * 1.5))            # h 交易日 ≈ 1.5h 日曆日
        lo = np.searchsorted(days, days - span, side="left")
        hi = np.searchsorted(days, days + span, side="right")
        counts = (hi - lo).astype(float)           # 含自己的重疊數
        idx_arr = np.asarray(list(idx))
        w.loc[idx_arr[order]] = 1.0 / counts
    return w.round(4)


def _build_big(hs: list[int], max_date: pd.Timestamp | None = None) -> pd.DataFrame | None:
    """掃全市場 parquet → 每檔長表 concat + 衍生欄（vs_index/sector_rs/regime）。最貴的一步。

    max_date：資料截止日（含），一路傳進 _symbol_long/_index_trailing，讓整份 big 只由
    trade_date <= max_date 的資料衍生（固定資料快照，供 eval_snapshot 的 A/B 對照）。
    """
    price_dir = PARQUET_ROOT / TW_MARKET
    if not price_dir.exists():
        return None
    idx = _index_trailing(max_date)
    frames: list[pd.DataFrame] = []
    syms = [p.stem for p in price_dir.glob("*.parquet") if p.stem != INDEX_SYMBOL]
    for sym in syms:
        try:
            fr = _symbol_long(sym, hs, max_date)
        except Exception as exc:  # noqa: BLE001 — 單檔壞資料不阻斷
            logger.debug("training_set 略過 %s: %s", sym, exc)
            continue
        if fr is not None:
            frames.append(fr)
    if not frames:
        return None
    with warnings.catch_warnings():
        # 多數 symbol 的基本面欄整欄 NaN（尚未回補）→ concat 觸發 pandas all-NA dtype 推斷
        # FutureWarning；結果 dtype 仍為 float、行為正確，這裡只壓掉 log 噪音。
        warnings.simplefilter("ignore", FutureWarning)
        big = pd.concat(frames, ignore_index=True)
    big.attrs["symbols_scanned"] = len(syms)
    big.attrs["symbols_used"] = len(frames)
    # 相對大盤強弱（短/中期）：個股報酬減同日大盤同窗報酬。
    big["vs_index_5d_pct"] = (big["return_5d_pct"]
                              - big["trade_date"].map(idx.get("trail_5", pd.Series(dtype=float)))).round(2)
    big["vs_index_20d_pct"] = (big["return_20d_pct"]
                               - big["trade_date"].map(idx.get("trail_20", pd.Series(dtype=float)))).round(2)
    # 產業相對強弱：個股 20 日報酬減同日同產業中位數（成員 <3 不算，與線上一致）。
    grp = big.groupby(["trade_date", "sector"])["return_20d_pct"]
    big["sector_rs_20d_pct"] = (big["return_20d_pct"] - grp.transform("median")).round(2).where(
        grp.transform("count") >= 3)
    # regime[9]：大盤趨勢/波動狀態（同日對所有股相同；serve 端走 current_market_regime 注入）。
    big["mkt_trend_20d_pct"] = big["trade_date"].map(idx.get("trend_20", pd.Series(dtype=float))).round(2)
    big["mkt_vol_20d_pct"] = big["trade_date"].map(idx.get("vol_20", pd.Series(dtype=float))).round(2)
    # 市場恐慌/避險（TAIFEX P/C ratio）：市場級同日對所有股同值（缺檔→欄全 NaN，HistGBT 原生吃）。
    from processor import market_regime  # noqa: PLC0415
    return _attach_pc_features(big, market_regime.pc_feature_frame())


def _attach_pc_features(big: pd.DataFrame, pc: pd.DataFrame) -> pd.DataFrame:
    """把市場級 P/C 特徵接到 big：merge_asof backward + **不允許同日匹配**。

    P/C 為當日盤後(~15:00)公布，date-D 樣本在 D 收盤進場時尚不知 D 的 P/C（用了＝洩漏半天）；
    故每個 date-D 取「trade_date 嚴格早於 D 的最近一筆」（＝D-1 的盤後值）。與 serve 端
    market_regime.latest_pc_features 的「known_date < 今日」同一規則，杜絕 train/serve skew。
    """
    from processor import market_regime  # noqa: PLC0415
    if pc is None or pc.empty:
        for c in market_regime.PC_FEATURES:
            big[c] = np.nan
        return big
    pc = pc[["trade_date", *market_regime.PC_FEATURES]].copy()
    pc["trade_date"] = pd.to_datetime(pc["trade_date"])
    pc = pc.sort_values("trade_date").reset_index(drop=True)
    big = big.sort_values("trade_date").reset_index(drop=True)
    return pd.merge_asof(big, pc, on="trade_date", direction="backward", allow_exact_matches=False)


def _emit_all(big: pd.DataFrame, hs: list[int], *, min_amount: float, max_amount: float | None) -> pd.DataFrame:
    """對 big 的每個交易日 emit 樣本（指定流動性帶）→ 加重疊權重 → 回 DataFrame（空回空）。"""
    samples: list[dict[str, Any]] = []
    for _d, day_df in big.groupby("trade_date"):
        samples.extend(_emit_samples(day_df, hs, min_amount=min_amount, max_amount=max_amount))
    if not samples:
        return pd.DataFrame()
    ds = pd.DataFrame(samples)
    ds["weight"] = _overlap_weights(ds)
    return ds


def build_training_set(hs: list[int] | None = None, *, min_amount: float = MIN_AMOUNT_TWD,
                       max_amount: float | None = None, out_path: Path = TRAINING_SET_PATH,
                       big: pd.DataFrame | None = None,
                       max_date: pd.Timestamp | str | None = None) -> dict[str, Any]:
    """掃全市場 parquet，回放選股規則產出歷史訓練集，落地 out_path。回統計。

    流動性帶預設＝主池（>=50M、無上限）；改戰場實驗傳中小型帶與另一 out_path（見 run_battlefield_experiment）。
    可傳已建好的 big 重用（省去重複掃描）。

    max_date：資料截止日（含）。給定時整份訓練集只由 trade_date <= max_date 的資料衍生，
    讓改動前後在同一份快照上對照（eval_snapshot 的 D1 歸因方法論）。
    """
    hs = hs or horizons()
    t0 = time.monotonic()
    if max_date is not None:
        max_date = pd.Timestamp(max_date)
    if big is None:
        big = _build_big(hs, max_date)
    if big is None or big.empty:
        return {"built": False, "reason": "no usable symbols"}
    ds = _emit_all(big, hs, min_amount=min_amount, max_amount=max_amount)
    if ds.empty:
        return {"built": False, "reason": "no samples (forward labels not matured yet)"}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_parquet(out_path, engine="pyarrow", index=False)

    per_h = {int(h): int((ds["horizon"] == h).sum()) for h in hs}
    stats = {
        "built": True,
        "rows": len(ds),
        "symbols_scanned": big.attrs.get("symbols_scanned"),
        "symbols_used": big.attrs.get("symbols_used"),
        "distinct_days": int(big["trade_date"].nunique()),
        "date_range": [ds["as_of"].min(), ds["as_of"].max()],
        "max_date": (max_date.date().isoformat() if max_date is not None else None),
        "samples_per_horizon": per_h,
        "band": [min_amount, max_amount],
        "elapsed_sec": round(time.monotonic() - t0, 1),
    }
    logger.info("歷史訓練集建好：%d 列、%d 天、各窗 %s、帶 %s（%.1fs）",
                stats["rows"], stats["distinct_days"], per_h, stats["band"], stats["elapsed_sec"])
    return stats


def load_training_set(h: int, path: Path = TRAINING_SET_PATH) -> pd.DataFrame:
    """讀回某窗的歷史樣本（無檔回空）。"""
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    return df[df["horizon"] == h].reset_index(drop=True)


def build_if_stale(max_age_days: float = 3.0) -> dict[str, Any]:
    """訓練集不存在或過舊（>max_age_days）才重建——讓每日晨報迴圈便宜地保持新鮮。"""
    if TRAINING_SET_PATH.exists():
        age_days = (time.time() - TRAINING_SET_PATH.stat().st_mtime) / 86400
        if age_days <= max_age_days:
            return {"built": False, "reason": f"fresh ({age_days:.1f}d)"}
    return build_training_set()
