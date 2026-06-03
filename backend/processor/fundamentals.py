"""基本面（M8-4 月營收 + M9 完整財報）：on-demand，對齊 design §5.2。

全市場 2000+ 檔不可能每天全抓財報，故對「焦點標的」（movers/watchlist/問答標的）
on-demand 抓 FinMind 月營收 + 季財報（損益/資產負債/現金流/股利）並算衍生指標。
process 內 lru 快取（月營收月更、季報季更，當日重複查不重抓）。

衍生指標（毛利率/營益率/淨利率/負債比/EPS TTM/自由現金流）一律由 FinMind 原始
數字推算，餵 AI 時以 features 路徑暴露，過 guardrail metric/source 驗證。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from config import settings

logger = logging.getLogger("ai-market-backend.fundamentals")

# ── 磁碟快取：把慢變動的基本面落地，跨重啟保留，依 TTL 自然汰換 ──
# 月營收（月更）與季財報（季更）各存一個 section；命中且未過期就不打 FinMind。
_CACHE_DIR = Path(settings.local_storage_path) / "cache" / "fundamentals"


def _cache_path(symbol: str) -> Path:
    return _CACHE_DIR / f"{symbol}.json"


# ── 日曆感知略過：依台股法定申報期限判斷「目前理應已公布的最新一期」──
# 季報截止：Q1→5/15、Q2(半年報)→8/14、Q3→11/14、Q4(年報)→隔年 3/31。
# 月營收截止：每月營收須於次月 10 日前公布。
# 只要快取已是「應公布的最新期別」就不重抓（無視 TTL）；唯有新一期理應公布卻還沒抓到才打 FinMind。

def _quarter_deadlines(year: int) -> list[tuple[str, date]]:
    """某年度各季報的（季別標籤, 申報截止日）。Q4 以年報截止日（隔年 3/31）計。"""
    return [
        (f"{year - 1}Q4", date(year, 3, 31)),
        (f"{year}Q1", date(year, 5, 15)),
        (f"{year}Q2", date(year, 8, 14)),
        (f"{year}Q3", date(year, 11, 14)),
    ]


def _latest_expected_quarter(today: date, buffer_days: int) -> str | None:
    """依今日日期回「(截止日+緩衝) 已到」的最新季別標籤（如 '2026Q1'）；都還沒到回 None。"""
    cands = (_quarter_deadlines(today.year - 1)
             + _quarter_deadlines(today.year)
             + _quarter_deadlines(today.year + 1))
    avail = [(lbl, dl) for lbl, dl in cands if dl + timedelta(days=buffer_days) <= today]
    return max(avail, key=lambda x: x[1])[0] if avail else None


def _rev_deadline(ry: int, rm: int) -> date:
    """某年月營收的申報截止日（次月 10 日）。"""
    ny, nm = (ry + 1, 1) if rm == 12 else (ry, rm + 1)
    return date(ny, nm, 10)


def _latest_expected_rev_month(today: date, buffer_days: int) -> str | None:
    """依今日日期回「(截止日+緩衝) 已到」的最新營收月標籤（如 '2026-04'）；都還沒到回 None。"""
    y, m = today.year, today.month
    cands: list[tuple[int, int]] = []
    for _ in range(6):  # 往回看半年足夠涵蓋公布落差
        cands.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    avail = [(ry, rm) for ry, rm in cands
             if _rev_deadline(ry, rm) + timedelta(days=buffer_days) <= today]
    if not avail:
        return None
    ry, rm = max(avail)
    return f"{ry}-{rm:02d}"


def _section_fresh(kind: str, data: Any, age_days: float, ttl_days: float) -> bool:
    """是否仍可沿用快取（不重抓）。

    財報/月營收走日曆感知：快取期別 ≥ 「理應已公布的最新期別」就視為最新、不重抓；
    缺期別資訊時退回時間 TTL。負快取（查無資料的標的）給長 TTL，不必每輪重探。
    """
    if data is None:
        return age_days <= settings.fundamentals_negcache_ttl_days

    today = date.today()
    buf = settings.fundamentals_filing_buffer_days
    if kind == "financials":
        have, target = data.get("fiscal_quarter"), _latest_expected_quarter(today, buf)
        if not have or not target:
            return age_days <= ttl_days
        return have >= target  # 期別字串等寬，字典序比較即時序比較
    if kind == "revenue":
        have, target = data.get("revenue_month"), _latest_expected_rev_month(today, buf)
        if not have or not target:
            return age_days <= ttl_days
        return have >= target
    return age_days <= ttl_days


def _read_section(symbol: str, kind: str, ttl_days: float) -> tuple[bool, Any]:
    """回 (命中且仍最新, data)；data 可為 None（負快取，避免重抓查無的標的）。"""
    p = _cache_path(symbol)
    if not p.exists():
        return False, None
    try:
        sec = json.loads(p.read_text(encoding="utf-8")).get(kind)
        fetched = datetime.fromisoformat(sec["fetched_at"])
    except Exception:  # noqa: BLE001 — 快取壞了就當 miss 重抓
        return False, None
    data = sec.get("data")
    age_days = (datetime.now(timezone.utc) - fetched).total_seconds() / 86400
    if _section_fresh(kind, data, age_days, ttl_days):
        return True, data
    return False, None


def _write_section(symbol: str, kind: str, data: Any) -> None:
    p = _cache_path(symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        blob = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        blob = {}
    blob[kind] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)  # 原子寫入，避免半寫壞檔


def _cached(symbol: str, kind: str, ttl_days: float,
            fetch_fn: Callable[[], Any], *, force: bool = False) -> Any:
    """磁碟快取包裝：命中未過期就回快取，否則抓取後落地。force=True 強制重抓。"""
    if not force:
        hit, data = _read_section(symbol, kind, ttl_days)
        if hit:
            return data
    data = fetch_fn()
    _write_section(symbol, kind, data)
    return data


def build_fundamentals(symbol: str, *, force: bool = False) -> dict[str, Any] | None:
    """單檔基本面：月營收 YoY/MoM（億元）+ 季財報摘要。查無回 None。

    走磁碟快取（月營收月更、季財報季更），命中未過期就不打 FinMind；force=True 強制重抓。
    """
    out: dict[str, Any] = {}
    rev = _build_revenue(symbol, force=force)
    if rev:
        out.update(rev)
    fin = build_financials(symbol, force=force)
    if fin:
        out.update(fin)
    return out or None


def _build_revenue(symbol: str, *, force: bool = False) -> dict[str, Any] | None:
    """月營收 + YoY/MoM（億元）；走磁碟快取（TTL=fundamentals_revenue_ttl_days）。"""
    return _cached(symbol, "revenue", settings.fundamentals_revenue_ttl_days,
                   lambda: _fetch_revenue(symbol), force=force)


def _fetch_revenue(symbol: str) -> dict[str, Any] | None:
    """實際打 FinMind 抓月營收（未過快取，由 _build_revenue 包裝）。"""
    from data_sources import finmind_loader

    try:
        start = (date.today().replace(year=date.today().year - 2)).isoformat()
        rows = finmind_loader.get_month_revenue(symbol, start)
    except finmind_loader.FinMindBackoff:
        raise  # 退避訊號往上拋：不寫負快取、讓慢爬中止本輪
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


def build_financials(symbol: str, *, force: bool = False) -> dict[str, Any] | None:
    """單檔季財報摘要（走磁碟快取，TTL=fundamentals_financials_ttl_days）。"""
    return _cached(symbol, "financials", settings.fundamentals_financials_ttl_days,
                   lambda: _fetch_financials(symbol), force=force)


def _fetch_financials(symbol: str) -> dict[str, Any] | None:
    """實際打 FinMind 抓季財報：EPS（最新季 + 近四季 TTM）、三率、負債比、營業/自由現金流、股利。

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
    except finmind_loader.FinMindBackoff:
        raise
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
    except finmind_loader.FinMindBackoff:
        raise
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
    except finmind_loader.FinMindBackoff:
        raise
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
    except finmind_loader.FinMindBackoff:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("股利抓取 %s 失敗: %s", symbol, exc)

    return out or None
