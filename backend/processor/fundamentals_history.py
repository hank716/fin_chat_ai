"""歷史『逐期』基本面（point-in-time）：供 edge 訓練集 leakage-safe 整併。

`processor/fundamentals.py` 只取「最新一期」快照；這裡對單檔抓 FinMind 全史，對每個營收月 / 財報季
各算一列衍生指標，並標上 **known_date＝該期法定申報截止日 + 緩衝**（資料『理應已公布』的日子）。
落地兩個 per-symbol parquet（月頻/季頻分檔，依 known_date 排序）：

    storage/local_parquet/tw/_fund_rev/{symbol}.parquet   ← 月營收 YoY/MoM
    storage/local_parquet/tw/_fund_fin/{symbol}.parquet   ← 季報三率/EPS TTM/負債比/FCF

訓練集（reports.training_set）再用 merge_asof(backward) 對每個 trade_date 取「當時 known_date 已到的
最新一期」，杜絕未來洩漏。打 FinMind（季/月頻、量不大），由 history_crawl Track C 慢爬、嚴守額度。
重用 fundamentals.py 的期限/解析函式，避免公式漂移。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from config import settings
from processor.fundamentals import (  # noqa: PLC2701 — 刻意重用同套期限/解析邏輯
    _pick,
    _pivot_by_date,
    _quarter_deadlines,
    _quarter_label,
    _rev_deadline,
)

logger = logging.getLogger("ai-market-backend.fundamentals_history")

_PARQUET_ROOT = Path(settings.local_storage_path) / "local_parquet" / "tw"
FUND_REV_DIR = _PARQUET_ROOT / "_fund_rev"
FUND_FIN_DIR = _PARQUET_ROOT / "_fund_fin"

# 落地的特徵欄位（與 backtest.FEATURE_COLUMNS 的基本面子集一致）
REV_FIELDS = ["revenue_yoy_pct", "revenue_mom_pct"]
FIN_FIELDS = ["eps_ttm", "gross_margin_pct", "operating_margin_pct",
              "net_margin_pct", "debt_ratio_pct", "free_cashflow_ttm_100m"]


def _buf() -> int:
    return settings.fundamentals_filing_buffer_days


def _quarter_known_date(qlabel: str) -> date | None:
    """季別標籤（如 '2026Q1'）→ 申報截止日 + 緩衝（資料理應已公布日）。重用 _quarter_deadlines。"""
    try:
        y = int(qlabel[:4])
    except (ValueError, TypeError):
        return None
    for yr in (y, y + 1):                         # Q4 出現在隔年度的截止表
        for lbl, dl in _quarter_deadlines(yr):
            if lbl == qlabel:
                return dl + timedelta(days=_buf())
    return None


def _ttm_ending_at(dates_sorted: list[str], by_date: dict[str, dict[str, float]],
                   i: int, *candidates: str) -> float | None:
    """sum 截至 dates_sorted[i] 的近四季某科目；不足四季或任一缺值回 None（TTM 用）。"""
    if i < 3:
        return None
    vals = []
    for d in dates_sorted[i - 3:i + 1]:
        v = _pick(by_date[d], *candidates)
        if v is None:
            return None
        vals.append(v)
    return sum(vals)


def _revenue_history(symbol: str, start: str, fm) -> list[dict[str, Any]]:
    """逐月營收 YoY/MoM + known_date（次月 10 日 + 緩衝）。"""
    rows = fm.get_month_revenue(symbol, start)    # FinMindBackoff 往上拋
    if not rows:
        return []
    by: dict[tuple[int, int], float] = {}
    for r in rows:
        y, m, v = r.get("revenue_year"), r.get("revenue_month"), r.get("revenue")
        if y and m and v not in (None, ""):
            try:
                by[(int(y), int(m))] = float(v)
            except (TypeError, ValueError):
                continue
    out = []
    for (y, m), rev in sorted(by.items()):
        prev = by.get((y, m - 1)) if m > 1 else by.get((y - 1, 12))
        yoy_base = by.get((y - 1, m))
        out.append({
            "known_date": pd.Timestamp(_rev_deadline(y, m) + timedelta(days=_buf())),
            "revenue_month": f"{y}-{m:02d}",
            "revenue_yoy_pct": round((rev / yoy_base - 1) * 100, 1) if yoy_base else None,
            "revenue_mom_pct": round((rev / prev - 1) * 100, 1) if prev else None,
        })
    return out


def _financials_history(symbol: str, start: str, fm) -> list[dict[str, Any]]:
    """逐季三率 / EPS TTM / 負債比 / FCF TTM + known_date（季截止日 + 緩衝）。"""
    fs = _pivot_by_date(fm.get_financial_statements(symbol, start))
    bs = _pivot_by_date(fm.get_balance_sheet(symbol, start))
    cf = _pivot_by_date(fm.get_cash_flows(symbol, start))
    if not fs:
        return []
    fdates = sorted(fs)
    cfdates = sorted(cf)
    out = []
    for i, d in enumerate(fdates):
        q = _quarter_label(d)
        kd = _quarter_known_date(q) if q else None
        if kd is None:
            continue
        items = fs[d]
        rev = _pick(items, "Revenue")
        gross = _pick(items, "GrossProfit")
        op = _pick(items, "OperatingIncome")
        net = _pick(items, "IncomeAfterTaxes", "ProfitLoss", "NetIncome")
        ttm = _ttm_ending_at(fdates, fs, i, "EPS", "BasicEarningsPerShare")
        row: dict[str, Any] = {
            "known_date": pd.Timestamp(kd),
            "fiscal_quarter": q,
            "eps_ttm": round(ttm, 2) if ttm is not None else None,
            "gross_margin_pct": round(gross / rev * 100, 1) if rev and gross is not None else None,
            "operating_margin_pct": round(op / rev * 100, 1) if rev and op is not None else None,
            "net_margin_pct": round(net / rev * 100, 1) if rev and net is not None else None,
            "debt_ratio_pct": None,
            "free_cashflow_ttm_100m": None,
        }
        if d in bs:
            assets = _pick(bs[d], "TotalAssets", "Total assets")
            liab = _pick(bs[d], "Liabilities", "TotalLiabilities")
            if assets and liab is not None:
                row["debt_ratio_pct"] = round(liab / assets * 100, 1)
        if d in cf:
            ci = cfdates.index(d)
            ocf = _ttm_ending_at(cfdates, cf, ci, "CashFlowsFromOperatingActivities")
            capex = _ttm_ending_at(cfdates, cf, ci,
                                   "PropertyAndPlantAndEquipment",
                                   "AcquisitionOfPropertyPlantAndEquipment")
            if ocf is not None and capex is not None:
                # FCF = 營業現金流 − 資本支出；capex 符號隨版本不一，取 abs 保守。
                row["free_cashflow_ttm_100m"] = round((ocf - abs(capex)) / 1e8, 1)
        out.append(row)
    return out


def _write(dir_path: Path, symbol: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    dir_path.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows).sort_values("known_date").reset_index(drop=True)
    df.to_parquet(dir_path / f"{symbol}.parquet", engine="pyarrow", index=False)
    return len(df)


def build_symbol_fundamentals_history(symbol: str) -> dict[str, int]:
    """抓單檔全史基本面 → 逐期衍生指標 + known_date → 落地兩 parquet。回寫入列數。

    FinMindBackoff（額度/封鎖）往上拋給 Track C 中止本輪；單一端點非退避錯誤已在各 history 函式內
    由 FinMind retry 處理，徹底失敗則該區塊空。
    """
    from data_sources import finmind_loader  # noqa: PLC0415

    start = (date.today() - timedelta(days=int(365 * 2.5))).isoformat()
    rev_rows = _revenue_history(symbol, start, finmind_loader)
    fin_rows = _financials_history(symbol, start, finmind_loader)
    n_rev = _write(FUND_REV_DIR, symbol, rev_rows)
    n_fin = _write(FUND_FIN_DIR, symbol, fin_rows)
    return {"symbol": symbol, "rev_rows": n_rev, "fin_rows": n_fin}


def read_history(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """讀回單檔 (月營收, 季報) 歷史（依 known_date 升冪）；無檔回空 DataFrame。供訓練集 merge_asof。"""
    def _read(dir_path: Path) -> pd.DataFrame:
        path = dir_path / f"{symbol}.parquet"
        if not path.exists():
            return pd.DataFrame()
        df = pd.read_parquet(path)
        df["known_date"] = pd.to_datetime(df["known_date"])
        return df.sort_values("known_date").reset_index(drop=True)
    return _read(FUND_REV_DIR), _read(FUND_FIN_DIR)
