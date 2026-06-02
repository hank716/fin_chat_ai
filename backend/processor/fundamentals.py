"""基本面（M8-4 月營收 + M9 完整財報）：on-demand，對齊 design §5.2。

全市場 2000+ 檔不可能每天全抓財報，故對「焦點標的」（movers/watchlist/問答標的）
on-demand 抓 FinMind 月營收 + 季財報（損益/資產負債/現金流/股利）並算衍生指標。
process 內 lru 快取（月營收月更、季報季更，當日重複查不重抓）。

衍生指標（毛利率/營益率/淨利率/負債比/EPS TTM/自由現金流）一律由 FinMind 原始
數字推算，餵 AI 時以 features 路徑暴露，過 guardrail metric/source 驗證。
"""
from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache
from typing import Any

logger = logging.getLogger("ai-market-backend.fundamentals")


@lru_cache(maxsize=2048)
def build_fundamentals(symbol: str) -> dict[str, Any] | None:
    """單檔基本面：月營收 YoY/MoM（億元）+ 季財報摘要。查無回 None。"""
    out: dict[str, Any] = {}
    rev = _build_revenue(symbol)
    if rev:
        out.update(rev)
    fin = build_financials(symbol)
    if fin:
        out.update(fin)
    return out or None


def _build_revenue(symbol: str) -> dict[str, Any] | None:
    """月營收 + YoY/MoM（億元）。"""
    from data_sources import finmind_loader

    try:
        start = (date.today().replace(year=date.today().year - 2)).isoformat()
        rows = finmind_loader.get_month_revenue(symbol, start)
    except Exception as exc:  # noqa: BLE001
        logger.warning("月營收抓取 %s 失敗: %s", symbol, exc)
        return None
    if not rows:
        return None

    rows = sorted(rows, key=lambda r: (r.get("revenue_year", 0), r.get("revenue_month", 0)))
    latest = rows[-1]
    rev = latest.get("revenue")
    if rev in (None, ""):
        return None
    y, m = latest.get("revenue_year"), latest.get("revenue_month")

    def _find(yy: int, mm: int) -> float | None:
        for r in rows:
            if r.get("revenue_year") == yy and r.get("revenue_month") == mm:
                v = r.get("revenue")
                return float(v) if v not in (None, "") else None
        return None

    rev = float(rev)
    prev_m = _find(y, m - 1) if m and m > 1 else _find((y or 0) - 1, 12)
    yoy_base = _find((y or 0) - 1, m)
    mom = round((rev / prev_m - 1) * 100, 1) if prev_m else None
    yoy = round((rev / yoy_base - 1) * 100, 1) if yoy_base else None
    return {
        "revenue_month": f"{y}-{m:02d}" if y and m else None,
        "revenue_100m": round(rev / 1e8, 1),   # 億元
        "revenue_yoy_pct": yoy,
        "revenue_mom_pct": mom,
    }


# ─────────────────────────── M9 完整財報 ───────────────────────────

def _pivot_by_date(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """FinMind 長格式（date/type/value）→ {date: {type: value}}。value 轉 float。"""
    by_date: dict[str, dict[str, float]] = {}
    for r in rows:
        d, t, v = r.get("date"), r.get("type"), r.get("value")
        if not d or not t or v in (None, ""):
            continue
        try:
            by_date.setdefault(d, {})[t] = float(v)
        except (TypeError, ValueError):
            continue
    return by_date


def _pick(items: dict[str, float], *candidates: str) -> float | None:
    """從一季的 {type: value} 取值；先精確比對，再 case-insensitive 子字串 fallback
    （FinMind type 字串偶有版本差異，做寬鬆比對較穩）。"""
    for c in candidates:
        if c in items:
            return items[c]
    low = {k.lower(): v for k, v in items.items()}
    for c in candidates:
        cl = c.lower()
        if cl in low:
            return low[cl]
        for k, v in low.items():
            if cl in k:
                return v
    return None


def _quarter_label(date_str: str) -> str | None:
    """'2026-03-31' → '2026Q1'。"""
    try:
        y, m, _ = date_str.split("-")
        q = (int(m) - 1) // 3 + 1
        return f"{y}Q{q}"
    except (ValueError, AttributeError):
        return None


def _sum_last_n(by_date: dict[str, dict[str, float]], n: int, *candidates: str) -> float | None:
    """取最近 n 季（依日期排序）的某科目加總（近四季 TTM 用）。"""
    vals = []
    for d in sorted(by_date)[-n:]:
        v = _pick(by_date[d], *candidates)
        if v is not None:
            vals.append(v)
    return sum(vals) if len(vals) == n else None


@lru_cache(maxsize=2048)
def build_financials(symbol: str) -> dict[str, Any] | None:
    """單檔季財報摘要：EPS（最新季 + 近四季 TTM）、三率、負債比、營業/自由現金流、股利。

    任一資料源失敗只略過該區塊，不影響其餘（每組 try/except 獨立）。全部查無回 None。
    """
    from data_sources import finmind_loader

    start = (date.today().replace(year=date.today().year - 2)).isoformat()
    out: dict[str, Any] = {}

    # ── 損益表：最新季三率 + EPS + 近四季 TTM EPS ──
    try:
        fs = _pivot_by_date(finmind_loader.get_financial_statements(symbol, start))
        dates = sorted(fs)
        if dates:
            latest = fs[dates[-1]]
            out["fiscal_quarter"] = _quarter_label(dates[-1])
            rev = _pick(latest, "Revenue")
            gross = _pick(latest, "GrossProfit")
            op = _pick(latest, "OperatingIncome")
            net = _pick(latest, "IncomeAfterTaxes", "ProfitLoss", "NetIncome")
            eps = _pick(latest, "EPS", "BasicEarningsPerShare")
            if eps is not None:
                out["eps_quarter"] = round(eps, 2)
            ttm = _sum_last_n(fs, 4, "EPS", "BasicEarningsPerShare")
            if ttm is not None:
                out["eps_ttm"] = round(ttm, 2)
            if rev:
                if gross is not None:
                    out["gross_margin_pct"] = round(gross / rev * 100, 1)
                if op is not None:
                    out["operating_margin_pct"] = round(op / rev * 100, 1)
                if net is not None:
                    out["net_margin_pct"] = round(net / rev * 100, 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("損益表抓取 %s 失敗: %s", symbol, exc)

    # ── 資產負債表：最新季負債比 ──
    try:
        bs = _pivot_by_date(finmind_loader.get_balance_sheet(symbol, start))
        if bs:
            latest = bs[sorted(bs)[-1]]
            assets = _pick(latest, "TotalAssets", "Total assets")
            liab = _pick(latest, "Liabilities", "TotalLiabilities")
            if assets and liab is not None:
                out["debt_ratio_pct"] = round(liab / assets * 100, 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("資產負債表抓取 %s 失敗: %s", symbol, exc)

    # ── 現金流量表：近四季營業現金流 + 自由現金流（億元）──
    try:
        cf = _pivot_by_date(finmind_loader.get_cash_flows(symbol, start))
        ocf = _sum_last_n(cf, 4, "CashFlowsFromOperatingActivities")
        capex = _sum_last_n(cf, 4, "PropertyAndPlantAndEquipment", "AcquisitionOfPropertyPlantAndEquipment")
        if ocf is not None:
            out["op_cashflow_ttm_100m"] = round(ocf / 1e8, 1)
            if capex is not None:
                # FCF = 營業現金流 − 資本支出；capex 符號隨版本不一，取 abs 保守
                out["free_cashflow_ttm_100m"] = round((ocf - abs(capex)) / 1e8, 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("現金流量表抓取 %s 失敗: %s", symbol, exc)

    # ── 股利政策：最新年度現金/股票股利（元/股）──
    try:
        dv = finmind_loader.get_dividend(symbol, start)
        if dv:
            latest = max(dv, key=lambda r: r.get("date", ""))

            def _num(*keys: str) -> float:
                tot = 0.0
                for k in keys:
                    v = latest.get(k)
                    try:
                        tot += float(v) if v not in (None, "") else 0.0
                    except (TypeError, ValueError):
                        pass
                return tot

            cash = _num("CashEarningsDistribution", "CashStatutorySurplus")
            stock = _num("StockEarningsDistribution", "StockStatutorySurplus")
            if cash or stock:
                out["dividend"] = {
                    "year": (latest.get("date") or "")[:4] or None,
                    "cash_per_share": round(cash, 2),
                    "stock_per_share": round(stock, 2),
                }
    except Exception as exc:  # noqa: BLE001
        logger.warning("股利抓取 %s 失敗: %s", symbol, exc)

    return out or None
