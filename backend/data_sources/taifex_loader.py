"""台期交所選擇權 Put/Call Ratio（市場恐慌/避險 gauge 來源）。

兩個來源：
  - 日常增量：TAIFEX OpenAPI `GET /v1/PutCallRatio`（JSON，但只回滾動 ~21 天）。
  - 歷史回填：TAIFEX `cht/3/pcRatioDown`（POST 日期區間，回 CSV，可拉 2 年）。CSV 取不到則退回 OpenAPI 21 天 seed。

落地 `storage/local_parquet/tw/_taifex/pcr.parquet`，依 trade_date upsert（仿 fundamentals_history 自寫 parquet）。
**市場級單一序列**（同日對所有股同值）：對橫斷面 risk/meta 標籤近乎無效，主要供市場 regime / 總曝險
（見 processor.market_regime）。對外請求過 rate_limiter（'taifex' bucket）。
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from config import settings
from data_sources import rate_limiter
from storage import local_store   # 原子寫入 / 壞檔自癒的共用 parquet I/O

logger = logging.getLogger("ai-market-backend.taifex")

_DIR = Path(settings.local_storage_path) / "local_parquet" / "tw" / "_taifex"
PCR_PATH = _DIR / "pcr.parquet"
# known_date＝該列資料「可被使用的日期」。TAIFEX P/C 為**當日盤後(~15:00)公布**，故 known_date
# 語意上等於 trade_date（當天收盤後才知）；下游一律用「entry_day 進場時只能用 known_date < entry_day
# 的最近一筆」對齊，杜絕『用當日盤後資料預測當日收盤進場』的半天洩漏（見 market_regime / training_set）。
COLUMNS = ["trade_date", "known_date", "pc_vol_ratio", "pc_oi_ratio", "put_vol", "call_vol", "put_oi", "call_oi"]

_OPENAPI_URL = "https://openapi.taifex.com.tw/v1/PutCallRatio"
_PCRATIO_URL = "https://www.taifex.com.tw/cht/3/pcRatio"   # POST 日期區間→回含資料表的 HTML
_TIMEOUT = 30.0


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _row(d: date, pv, cv, pcv, po, co) -> dict[str, Any]:
    """組標準列：量比/OI比 + 原始量（OI 比是恐慌主訊號）。"""
    return {
        "trade_date": pd.Timestamp(d),
        "known_date": pd.Timestamp(d),                   # 當日盤後公布：known_date = trade_date
        "pc_vol_ratio": _num(pcv), "pc_oi_ratio": None,  # OI 比下面填
        "put_vol": _num(pv), "call_vol": _num(cv),
        "put_oi": _num(po), "call_oi": _num(co),
    }


def fetch_pc_recent() -> pd.DataFrame:
    """OpenAPI 抓滾動 ~21 天 → 標準欄 DataFrame（日常增量）。"""
    rate_limiter.acquire("taifex")
    resp = httpx.get(_OPENAPI_URL, timeout=_TIMEOUT)
    resp.raise_for_status()
    rows: list[dict[str, Any]] = []
    for r in resp.json():
        ds = str(r.get("Date") or "")
        if len(ds) != 8 or not ds.isdigit():
            continue
        d = date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
        row = _row(d, r.get("PutVolume"), r.get("CallVolume"), r.get("PutCallVolumeRatio%"),
                   r.get("PutOI"), r.get("CallOI"))
        row["pc_oi_ratio"] = _num(r.get("PutCallOIRatio%"))
        rows.append(row)
    return pd.DataFrame(rows, columns=COLUMNS)


def _parse_pc_html(html: str) -> list[dict[str, Any]]:
    """從 pcRatio 頁面 HTML 抽資料列（正則，不需 lxml）。欄序：日期, 賣權量, 買權量, 量比%, 賣權OI, 買權OI, OI比%。"""
    rows: list[dict[str, Any]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).replace("\xa0", " ").strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 7:
            continue
        m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", cells[0])    # 日期如 2026/6/18（非補零）
        if not m:
            continue
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        row = _row(d, cells[1], cells[2], cells[3], cells[4], cells[5])
        row["pc_oi_ratio"] = _num(cells[6])
        rows.append(row)
    return rows


def fetch_pc_history(start: date, end: date) -> pd.DataFrame:
    """POST pcRatio 日期區間 → 解析 HTML 表格回填。

    ⚠️ TAIFEX pcRatio 頁對「長區間」會回空（內部上限）；實測 ~2 週短窗對任何年份都有效，故按 14 天分塊
    （2 年 ~56 次請求、過 rate_limiter）。用同一 Client 先 GET 建 session + UA header（缺一則回空）。失敗回空。
    """
    rows: list[dict[str, Any]] = []
    headers = {"User-Agent": "Mozilla/5.0", "Referer": _PCRATIO_URL}
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers) as client:
            client.get(_PCRATIO_URL)                          # 建 session cookie
            cur = start
            while cur <= end:
                chunk_end = min(end, cur + timedelta(days=14))
                rate_limiter.acquire("taifex")
                try:
                    resp = client.post(_PCRATIO_URL, data={
                        "queryStartDate": cur.strftime("%Y/%m/%d"),
                        "queryEndDate": chunk_end.strftime("%Y/%m/%d"),
                    })
                    resp.raise_for_status()
                    rows.extend(_parse_pc_html(resp.content.decode("utf-8", errors="ignore")))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("TAIFEX pcRatio 區間 %s~%s 失敗: %s", cur, chunk_end, exc)
                cur = chunk_end + timedelta(days=1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("TAIFEX pcRatio session 失敗: %s", exc)
    return pd.DataFrame(rows, columns=COLUMNS)


def _upsert(df: pd.DataFrame) -> int:
    """依 trade_date upsert 進 pcr.parquet，回總列數。"""
    df = df.dropna(subset=["trade_date"]).copy()
    if df.empty:
        return 0
    if PCR_PATH.exists():
        old = local_store.read_parquet_safe(PCR_PATH, COLUMNS)
        if not old.empty:
            df = pd.concat([old, df], ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = (df.drop_duplicates(subset=["trade_date"], keep="last")
            .sort_values("trade_date").reset_index(drop=True))
    _DIR.mkdir(parents=True, exist_ok=True)
    local_store.write_parquet_atomic(df, PCR_PATH)
    return len(df)


def refresh_recent() -> dict[str, Any]:
    """日常增量：OpenAPI → upsert。guarded（失敗不阻斷晨報）。"""
    try:
        df = fetch_pc_recent()
        n = _upsert(df)
        logger.info("TAIFEX P/C 增量：+%d 列、總 %d 列", len(df), n)
        return {"refreshed": True, "fetched": len(df), "total": n}
    except Exception as exc:  # noqa: BLE001
        logger.warning("TAIFEX P/C 增量失敗：%s", exc)
        return {"refreshed": False, "reason": str(exc)}


def backfill(years: int = 2) -> dict[str, Any]:
    """一次性回填 ~years 年（CSV 區間）；CSV 失敗則退回 OpenAPI 21 天 seed。"""
    end = date.today()
    start = end - timedelta(days=int(years * 365.25))
    hist = fetch_pc_history(start, end)
    used = "csv"
    if hist.empty or len(hist) < 100:                     # CSV 不可用/太少 → 退回 OpenAPI
        hist = fetch_pc_recent()
        used = "openapi_seed"
    n = _upsert(hist)
    logger.info("TAIFEX P/C 回填（%s）：+%d 列、總 %d 列", used, len(hist), n)
    return {"backfilled": True, "source": used, "fetched": len(hist), "total": n,
            "date_range": [start.isoformat(), end.isoformat()]}


def read_pcr() -> pd.DataFrame:
    """讀回 P/C 序列（不存在回空）。舊 parquet 缺 known_date 欄時回填為 trade_date（同語意）。"""
    if not PCR_PATH.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = local_store.read_parquet_safe(PCR_PATH, COLUMNS)
    if "known_date" not in df.columns:                   # 相容既有無 known_date 的落地檔
        df["known_date"] = pd.to_datetime(df["trade_date"])
    return df
