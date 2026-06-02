"""FinMind sync client — 對應設計文件 26 §6。

只給 Celery worker 使用 (sync), FastAPI 路由禁止直接打 (§11 規則)。
所有對外請求先過 rate_limiter.acquire('finmind')。
"""
from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from config import settings
from . import rate_limiter

BASE_URL = "https://api.finmindtrade.com/api/v4/data"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class FinMindError(RuntimeError):
    pass


class FinMindIPBanned(FinMindError):
    """FinMind 因超量暫時封鎖本機 IP（HTTP 403 `ip banned`）。

    `retry_after` 為 FinMind 回報的「還需等候秒數」，到期後自動解除。
    刻意不在 `_request` 層做快速重試——封鎖通常長達數百~上千秒，
    秒級重試只會持續打 API、無助於解除，交由呼叫端（backfill）決定等候/中止。
    """

    def __init__(self, retry_after: int, detail: str = "") -> None:
        self.retry_after = retry_after
        msg = f"FinMind IP banned, retry_after={retry_after}s"
        super().__init__(f"{msg} {detail}".strip())


def _should_retry(exc: BaseException) -> bool:
    """transient 錯誤（連線/逾時/一般 FinMindError）可重試；IP 封鎖不重試。"""
    if isinstance(exc, FinMindIPBanned):
        return False
    return isinstance(exc, (httpx.RequestError, FinMindError))


@retry(
    retry=retry_if_exception(_should_retry),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _request(dataset: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rate_limiter.acquire("finmind")
    payload = {"dataset": dataset, "token": settings.finmind_token, **(params or {})}
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(BASE_URL, params=payload)
    if resp.status_code != 200:
        detail = resp.text[:200]
        if resp.status_code == 403:
            # FinMind 超量封鎖：{"msg":"ip banned","status":403,"retry_after":1653}
            try:
                j = resp.json()
            except ValueError:
                j = {}
            if "ban" in str(j.get("msg", "")).lower():
                raise FinMindIPBanned(int(j.get("retry_after") or 0), detail)
        raise FinMindError(f"HTTP {resp.status_code}: {detail}")
    body = resp.json()
    if body.get("status") != 200:
        raise FinMindError(f"FinMind status={body.get('status')} msg={body.get('msg')}")
    return body.get("data") or []


def get_taiwan_stock_info() -> list[dict[str, Any]]:
    """Universe: 全 TWSE / TPEx 股票 + ETF 基本資料。

    回傳每筆: stock_id / stock_name / industry_category / type (twse/tpex) / date
    """
    return _request("TaiwanStockInfo")


def get_daily_prices(stock_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """單檔日 OHLCV。chunk 1.1 暫不批次抓全市場 (留 1.1b 用 TWSE MI_INDEX 一次拿)。"""
    return _request(
        "TaiwanStockPrice",
        {"data_id": stock_id, "start_date": start_date, "end_date": end_date},
    )


def fetch_stock_prices_normalized(
    stock_id: str, start_date: str, end_date: str
) -> list[Any]:
    """單檔歷史 OHLCV → 統一輸出 PriceRow (對應 services.twse.PriceRow)。

    用於 chunk 2.2 前置歷史 backfill (R-1.1b: TPEx daily endpoint 不支援歷史,
    改 FinMind 個股級)。FinMind 免費 tier 不能 market-wide 拉, 只能 per-stock。

    輸出 source='finmind'。FinMind volume 已是「股」, 不需要 ×1000 轉換 (與 TWSE T86 不同)。
    """
    from datetime import datetime as _dt
    from decimal import Decimal

    from .twse_loader import PriceRow

    raw = get_daily_prices(stock_id, start_date, end_date)
    out: list[PriceRow] = []
    for r in raw:
        try:
            d = _dt.strptime(r["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        out.append(
            PriceRow(
                symbol=stock_id,
                trade_date=d,
                open=_to_decimal(r.get("open")),
                high=_to_decimal(r.get("max")),
                low=_to_decimal(r.get("min")),
                close=_to_decimal(r.get("close")),
                volume=int(r["Trading_Volume"]) if r.get("Trading_Volume") not in (None, "") else None,
                amount=_to_decimal(r.get("Trading_money")),
                source="finmind",
            )
        )
    return out


def get_month_revenue(stock_id: str, start_date: str) -> list[dict[str, Any]]:
    """單檔月營收（revenue 元 / revenue_year / revenue_month）。"""
    return _request(
        "TaiwanStockMonthRevenue",
        {"data_id": stock_id, "start_date": start_date},
    )


def get_financial_statements(stock_id: str, start_date: str) -> list[dict[str, Any]]:
    """單檔綜合損益表（季）。長格式：每列 date/stock_id/type/value/origin_name。

    type 常見：Revenue（營收）/ CostOfGoodsSold / GrossProfit（毛利）/
    OperatingExpenses / OperatingIncome（營業利益）/ PreTaxIncome /
    IncomeAfterTaxes（稅後淨利）/ EPS（每股盈餘）。
    """
    return _request(
        "TaiwanStockFinancialStatements",
        {"data_id": stock_id, "start_date": start_date},
    )


def get_balance_sheet(stock_id: str, start_date: str) -> list[dict[str, Any]]:
    """單檔資產負債表（季）。長格式：每列 date/stock_id/type/value/origin_name。

    type 常見：TotalAssets（總資產）/ Liabilities（總負債）/ Equity（權益）。
    """
    return _request(
        "TaiwanStockBalanceSheet",
        {"data_id": stock_id, "start_date": start_date},
    )


def get_cash_flows(stock_id: str, start_date: str) -> list[dict[str, Any]]:
    """單檔現金流量表（季）。長格式：每列 date/stock_id/type/value/origin_name。

    type 常見：CashFlowsFromOperatingActivities（營業現金流）/
    PropertyAndPlantAndEquipment（資本支出，現金流出為負）/
    CashFlowsFromInvestingActivities / CashFlowsProvidedFromFinancingActivities。
    """
    return _request(
        "TaiwanStockCashFlowsStatement",
        {"data_id": stock_id, "start_date": start_date},
    )


def get_dividend(stock_id: str, start_date: str) -> list[dict[str, Any]]:
    """單檔股利政策（年）。含現金/股票股利欄位（CashEarningsDistribution 等）。"""
    return _request(
        "TaiwanStockDividend",
        {"data_id": stock_id, "start_date": start_date},
    )


def get_taiwan_stock_news(stock_id: str, start_date: str) -> list[dict[str, Any]]:
    """單檔近期新聞（date / stock_id / link / source / title）。"""
    return _request("TaiwanStockNews", {"data_id": stock_id, "start_date": start_date})


def get_stock_chip(stock_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """單檔三大法人買賣超（per-investor 的 buy/sell raw 列，單位股）。"""
    return _request(
        "TaiwanStockInstitutionalInvestorsBuySell",
        {"data_id": stock_id, "start_date": start_date, "end_date": end_date},
    )


# FinMind investor name → 我方三大法人分類
_FOREIGN = {"Foreign_Investor", "Foreign_Dealer_Self"}
_TRUST = {"Investment_Trust"}
_DEALER = {"Dealer_self", "Dealer_Hedging"}


def fetch_stock_chip_normalized(
    stock_id: str, start_date: str, end_date: str
) -> list[Any]:
    """單檔三大法人買賣超 → 統一輸出 ChipRow（每個 trade_date 一列）。

    FinMind 回 per-investor 的 buy/sell（單位股），這裡按日聚合成淨買賣超：
      foreign = 外資 + 外資自營；trust = 投信；dealer = 自營自行 + 自營避險。
    對齊 twse_loader.ChipRow（含外資自營、自營含自行+避險）。source='finmind'。
    """
    from datetime import datetime as _dt

    from .twse_loader import ChipRow

    raw = get_stock_chip(stock_id, start_date, end_date)
    # date -> {'foreign': net, 'trust': net, 'dealer': net}
    by_date: dict[str, dict[str, int]] = {}
    for r in raw:
        name = r.get("name")
        try:
            net = int(r.get("buy") or 0) - int(r.get("sell") or 0)
        except (TypeError, ValueError):
            continue
        d = r.get("date")
        if not d:
            continue
        bucket = by_date.setdefault(d, {"foreign": 0, "trust": 0, "dealer": 0})
        if name in _FOREIGN:
            bucket["foreign"] += net
        elif name in _TRUST:
            bucket["trust"] += net
        elif name in _DEALER:
            bucket["dealer"] += net

    out: list[ChipRow] = []
    for d, b in sorted(by_date.items()):
        try:
            td = _dt.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        out.append(
            ChipRow(
                symbol=stock_id,
                trade_date=td,
                foreign_net_buy=b["foreign"],
                trust_net_buy=b["trust"],
                dealer_net_buy=b["dealer"],
                source="finmind",
            )
        )
    return out


def get_stock_margin(stock_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """單檔融資融券（餘額單位：張）。"""
    return _request(
        "TaiwanStockMarginPurchaseShortSale",
        {"data_id": stock_id, "start_date": start_date, "end_date": end_date},
    )


def fetch_stock_margin_normalized(
    stock_id: str, start_date: str, end_date: str
) -> list[Any]:
    """單檔融資融券 → 統一輸出 MarginRow（餘額換成『股』對齊 twse_loader.MarginRow）。

    FinMind 餘額單位為張，×1000 轉股。margin_balance=融資今日餘額、
    short_balance=融券今日餘額。source='finmind'。
    """
    from datetime import datetime as _dt

    from .twse_loader import MarginRow

    raw = get_stock_margin(stock_id, start_date, end_date)
    out: list[MarginRow] = []
    for r in raw:
        try:
            d = _dt.strptime(r["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue

        def _shares(key: str) -> int | None:
            v = r.get(key)
            if v in (None, ""):
                return None
            try:
                return int(v) * 1000
            except (TypeError, ValueError):
                return None

        out.append(
            MarginRow(
                symbol=stock_id,
                trade_date=d,
                margin_balance=_shares("MarginPurchaseTodayBalance"),
                short_balance=_shares("ShortSaleTodayBalance"),
                source="finmind",
            )
        )
    return out


def _to_decimal(v: Any) -> Any:
    from decimal import Decimal, InvalidOperation

    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
