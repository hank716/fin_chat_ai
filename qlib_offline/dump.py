"""raw OHLCV parquet → Qlib `.bin` 二進位格式（自足，不 import backend，也不依賴 qlib scripts）。

Qlib bin 格式（calendar-indexed，穩定且有官方讀回器驗證）：
  calendars/day.txt        每行一個交易日（YYYY-MM-DD），全市場聯集、排序。
  instruments/all.txt      每行 `代號<TAB>起日<TAB>迄日`。
  features/{sym}/{f}.day.bin  float32 陣列：第 0 個值＝該檔首筆在 calendar 的位置(index)，
                              其後為從該位置起「連續 calendar 對齊」的欄位值（缺日填 NaN）。

落地後用 qlib 自身讀回抽檢一檔 $close 與來源 parquet 比對，確保格式正確（catch off-by-one）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import (
    INDEX_SYMBOL,
    PARQUET_TW,
    QLIB_DATA,
    available_symbols,
    load_adj_index,
    log,
)

# Alpha158 會用到的欄位：$open/$high/$low/$close/$volume/$vwap。
_FIELDS = ["open", "high", "low", "close", "volume", "vwap"]
# 價格欄位（除權息還原對象；volume 不還原）。vwap 一併還原以與 close 同基準。
_PRICE_FIELDS = ("open", "high", "low", "close", "vwap")


def _adj_mult(dates: pd.DatetimeIndex, adj: tuple) -> np.ndarray:
    """該檔各 trade_date 的還原倍率 = ∏{ex_date > t} factor = total / ∏{ex_date <= t}。"""
    ex, cum, total = adj
    idx = np.searchsorted(ex, dates.to_numpy(), side="right")
    prev = np.where(idx > 0, cum[np.clip(idx - 1, 0, len(cum) - 1)], 1.0)
    return total / prev


def _load_symbol(sym: str, adj_index: dict) -> pd.DataFrame | None:
    """讀單檔 parquet → 以日期(normalized)為 index 的 OHLCV+vwap（除權息還原）；資料太短回 None。"""
    path = PARQUET_TW / f"{sym}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if df.empty or "close" not in df.columns:
        return None
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
    df = df.dropna(subset=["trade_date"]).drop_duplicates("trade_date", keep="last")
    df = df.sort_values("trade_date").set_index("trade_date")
    vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series(np.nan, index=df.index)
    amt = df["amount"].astype(float) if "amount" in df.columns else pd.Series(np.nan, index=df.index)
    out = pd.DataFrame(index=df.index)
    for f in ("open", "high", "low", "close"):
        out[f] = df[f].astype(float) if f in df.columns else np.nan
    out["volume"] = vol
    out["vwap"] = np.where(vol > 0, amt / vol, np.nan)   # 成交均價≈成交金額/股數
    # 除權息 backward 還原（spec 017 / WP2.1）：對 OHLC+vwap 套倍率，消除除息跳空；volume 不動。
    adj = adj_index.get(sym)
    if adj is not None:
        mult = _adj_mult(out.index, adj)
        for f in _PRICE_FIELDS:
            out[f] = out[f].to_numpy(dtype="float64") * mult
    return out if len(out) >= 25 else None                # 至少能算 Alpha158 的 20 日窗


def dump() -> dict:
    """掃 available_symbols → 寫 Qlib bin store 到 QLIB_DATA。回統計。"""
    syms = available_symbols()
    adj_index = load_adj_index()
    frames: dict[str, pd.DataFrame] = {}
    for sym in syms:
        try:
            sd = _load_symbol(sym, adj_index)
        except Exception as exc:  # noqa: BLE001 — 單檔壞資料不阻斷
            log.debug("dump 略過 %s: %s", sym, exc)
            continue
        if sd is not None:
            frames[sym] = sd
    if not frames:
        return {"dumped": False, "reason": "no usable symbols"}

    # 全市場交易日聯集＝Qlib calendar。
    all_dates = sorted({d for sd in frames.values() for d in sd.index})
    cal_index = {d: i for i, d in enumerate(all_dates)}

    feat_root = QLIB_DATA / "features"
    cal_dir = QLIB_DATA / "calendars"
    inst_dir = QLIB_DATA / "instruments"
    for p in (feat_root, cal_dir, inst_dir):
        p.mkdir(parents=True, exist_ok=True)

    # calendars/day.txt
    (cal_dir / "day.txt").write_text(
        "\n".join(d.strftime("%Y-%m-%d") for d in all_dates) + "\n", encoding="utf-8")

    inst_lines: list[str] = []
    for sym, sd in frames.items():
        start_pos = cal_index[sd.index[0]]
        end_pos = cal_index[sd.index[-1]]
        # reindex 到 [start, end] 的「連續 calendar 切片」，缺日填 NaN（Qlib bin 要求連續對齊）。
        cal_slice = all_dates[start_pos:end_pos + 1]
        aligned = sd.reindex(cal_slice)
        sym_dir = feat_root / sym.lower()
        sym_dir.mkdir(parents=True, exist_ok=True)
        for f in _FIELDS:
            vals = aligned[f].to_numpy(dtype="float32")
            # 第 0 元素＝首筆 calendar 位置（float32）；其後為連續值。
            np.hstack([np.float32(start_pos), vals]).astype("<f4").tofile(
                sym_dir / f"{f}.day.bin")
        inst_lines.append(
            f"{sym}\t{sd.index[0].strftime('%Y-%m-%d')}\t{sd.index[-1].strftime('%Y-%m-%d')}")

    (inst_dir / "all.txt").write_text("\n".join(inst_lines) + "\n", encoding="utf-8")

    stats = {
        "dumped": True,
        "symbols": len(frames),
        "calendar_days": len(all_dates),
        "date_range": [all_dates[0].strftime("%Y-%m-%d"), all_dates[-1].strftime("%Y-%m-%d")],
        "qlib_dir": str(QLIB_DATA),
    }
    log.info("Qlib bin 落地：%d 檔 / %d 交易日 / %s",
             stats["symbols"], stats["calendar_days"], stats["date_range"])
    ok = _readback_check(frames)
    stats["readback_ok"] = ok
    if not ok:
        log.warning("⚠️ readback 自檢未通過——bin 格式可能有誤，後續評估數字不可信")
    return stats


def _readback_check(frames: dict[str, pd.DataFrame]) -> bool:
    """用 qlib 自身讀回一檔 $close，與來源 parquet 末筆比對（catch 格式/off-by-one）。"""
    try:
        import qlib  # noqa: PLC0415
        from qlib.data import D  # noqa: PLC0415

        qlib.init(provider_uri=str(QLIB_DATA), region="cn", expression_cache=None,
                  dataset_cache=None, redis_port=-1)
        sym = next(iter(frames))
        src = frames[sym]["close"].dropna()
        if src.empty:
            return True
        got = D.features([sym], ["$close"], start_time=src.index[0], end_time=src.index[-1])
        if got is None or got.empty:
            log.warning("readback：qlib 讀回 %s 為空", sym)
            return False
        last = float(got["$close"].dropna().iloc[-1])
        exp = float(src.iloc[-1])
        diff = abs(last - exp) / (abs(exp) + 1e-9)
        log.info("readback %s：qlib $close=%.4f vs parquet=%.4f（相對差 %.2e）", sym, last, exp, diff)
        return diff < 1e-3
    except Exception as exc:  # noqa: BLE001
        log.warning("readback 自檢失敗（qlib 未就緒？）: %s", exc)
        return False


if __name__ == "__main__":
    print(dump())
