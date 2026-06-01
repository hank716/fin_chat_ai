"""TWSE / TPEx daily quote + chip clients — 對應設計 26 §4 §7 + 22 §3 chip_data。

兩個來源都回 JSON 但 schema 不同, 統一輸出 PriceRow / ChipRow:
    PriceRow {symbol, trade_date, open, high, low, close, volume (股), amount (元), source}
    ChipRow  {symbol, trade_date, foreign_net_buy, trust_net_buy, dealer_net_buy, source}

歷史日期支援:
- TWSE MI_INDEX (價): 完整支援歷史 (date=YYYYMMDD 西元)
- TWSE T86      (籌): 完整支援歷史 (date=YYYYMMDD 西元)
- TPEx daily_close_quotes (價): endpoint 永遠回今日, 不支援歷史
- TPEx 3itrade_hedge_result (籌): 支援歷史 (d=YYY/MM/DD 民國)

兩個 API 都回應 stat / status，失敗會抛 TWSEError。
所有對外請求先過 rate_limiter.acquire('twse'/'tpex')。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import rate_limiter

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
TWSE_MI_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_MI_MARGN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TPEX_DAILY = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
TPEX_T86 = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
TPEX_MARGIN = "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php"

# TWSE 個股表 (table[8]) 欄位順序 — 對應 2024+ MI_INDEX schema
# fields: 證券代號/名稱/成交股數/成交筆數/成交金額/開盤/最高/最低/收盤/漲跌/漲跌價差/...
TWSE_TABLE_TITLE_PREFIX = "每日收盤行情"

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class TWSEError(RuntimeError):
    pass


class TWSENoDataError(RuntimeError):
    """API 回 'stat=很抱歉，沒有符合條件的資料' (該日尚未公布或非交易日)。

    刻意不繼承 TWSEError, 讓 tenacity 的 retry_if_exception_type((..., TWSEError))
    不會匹配 — 該日無資料時 retry 同一天無意義, caller 應 fallback 到 T-1。
    """
    pass


@dataclass(frozen=True)
class PriceRow:
    symbol: str
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: int | None
    amount: Decimal | None
    source: str

    def is_valid(self) -> tuple[bool, str | None]:
        """Data quality 基本檢查 — 對應設計 26 §12。

        失敗回 (False, reason); 通過回 (True, None)。caller 自行決定 skip 或 raise。
        """
        if self.close is None or self.close <= 0:
            return False, "close <= 0 or null"
        if self.high is not None and self.low is not None and self.high < self.low:
            return False, f"high({self.high}) < low({self.low})"
        for label, val in (("open", self.open), ("high", self.high), ("low", self.low)):
            if val is not None and val <= 0:
                return False, f"{label} <= 0"
        if self.high is not None:
            for label, val in (("open", self.open), ("close", self.close), ("low", self.low)):
                if val is not None and val > self.high:
                    return False, f"{label}({val}) > high({self.high})"
        if self.low is not None:
            for label, val in (("open", self.open), ("close", self.close), ("high", self.high)):
                if val is not None and val < self.low:
                    return False, f"{label}({val}) < low({self.low})"
        if self.volume is not None and self.volume < 0:
            return False, f"volume < 0 ({self.volume})"
        return True, None


# --- helpers ---------------------------------------------------------------

def _to_int(s: Any) -> int | None:
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "--", "---", "X"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _to_decimal(s: Any) -> Decimal | None:
    if s is None:
        return None
    s = _HTML_TAG_RE.sub("", str(s)).strip().replace(",", "")
    if s in ("", "--", "---", "X"):
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _twse_date_str(d: date) -> str:
    return d.strftime("%Y%m%d")


def _tpex_date_str(d: date) -> str:
    """TPEx 用民國年 YYY/MM/DD。注意 endpoint 對歷史會 silently fallback 到今日。"""
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def _parse_response_date(s: str | None) -> date:
    if not s:
        raise TWSEError("API 沒回 date")
    return datetime.strptime(s, "%Y%m%d").date()


def _assert_reasonable_date(requested: date, actual: date, max_diff_days: int = 30) -> None:
    """防 TWSE/TPEx 偶發回不合理日期 (曾觀察到對 20260506 偶爾回 20311122)。

    若實際日期離 requested > max_diff_days, 抛 TWSEError 觸發 tenacity retry。
    """
    diff = abs((actual - requested).days)
    if diff > max_diff_days:
        raise TWSEError(
            f"API 回的 date={actual} 偏離 requested={requested} {diff} 天 (>30), 視為 glitch"
        )


# --- public API ------------------------------------------------------------

@retry(
    retry=retry_if_exception_type((httpx.RequestError, TWSEError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def fetch_twse_daily(trade_date: date) -> tuple[date, list[PriceRow]]:
    """抓 TWSE 上市全市場個股 OHLCV。

    回 (actual_date, rows)。actual_date 用 endpoint 自己回的, 因為若傳的日期非交易日
    TWSE 會 fallback 或回 stat!=OK。
    """
    rate_limiter.acquire("twse")
    params = {"date": _twse_date_str(trade_date), "type": "ALLBUT0999", "response": "json"}
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "finflow-ai/0.1"}) as client:
        resp = client.get(TWSE_MI_INDEX, params=params)
    if resp.status_code != 200:
        raise TWSEError(f"TWSE HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    stat = body.get("stat")
    if stat != "OK":
        # "很抱歉，沒有符合條件的資料" = 假日 / 該日尚未公布 — 與 chip / margin 一致改抛
        # TWSENoDataError, 讓 tenacity 不 retry (同一天 retry 無意義), caller 自行 fallback。
        if stat and "沒有符合條件" in stat:
            raise TWSENoDataError(f"TWSE no data for date={trade_date}")
        raise TWSEError(f"TWSE stat={stat} for date={trade_date}")
    actual_date = _parse_response_date(body.get("date"))
    _assert_reasonable_date(trade_date, actual_date)

    # title 前面有民國日期, 用子字串比對
    table = next(
        (t for t in body.get("tables", []) if TWSE_TABLE_TITLE_PREFIX in (t.get("title") or "")),
        None,
    )
    if table is None:
        raise TWSEError(f"TWSE 找不到含 '{TWSE_TABLE_TITLE_PREFIX}' 的 table")

    rows: list[PriceRow] = []
    for r in table.get("data", []):
        if len(r) < 9:
            continue
        rows.append(
            PriceRow(
                symbol=str(r[0]).strip(),
                trade_date=actual_date,
                volume=_to_int(r[2]),
                amount=_to_decimal(r[4]),
                open=_to_decimal(r[5]),
                high=_to_decimal(r[6]),
                low=_to_decimal(r[7]),
                close=_to_decimal(r[8]),
                source="twse",
            )
        )
    return actual_date, rows


@retry(
    retry=retry_if_exception_type((httpx.RequestError, TWSEError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def fetch_tpex_daily(trade_date: date) -> tuple[date, list[PriceRow]]:
    """抓 TPEx 上櫃全市場個股 OHLCV。

    注意: TPEx 此 endpoint 對歷史日期會 silently 回今日資料 → 一律以 response.date 為主。
    """
    rate_limiter.acquire("tpex")
    params = {"l": "zh-tw", "d": _tpex_date_str(trade_date), "se": "AL", "s": "0,asc,0"}
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "finflow-ai/0.1"}) as client:
        resp = client.get(TPEX_DAILY, params=params)
    if resp.status_code != 200:
        raise TWSEError(f"TPEx HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    if body.get("stat") not in ("ok", "OK"):
        raise TWSEError(f"TPEx stat={body.get('stat')} for date={trade_date}")
    actual_date = _parse_response_date(body.get("date"))
    _assert_reasonable_date(trade_date, actual_date)

    tables = body.get("tables") or []
    if not tables:
        return actual_date, []
    main = tables[0]
    # 19 fields: 代號/名稱/收盤/漲跌/開盤/最高/最低/均價/成交股數/成交金額(元)/...
    rows: list[PriceRow] = []
    for r in main.get("data", []):
        if len(r) < 10:
            continue
        rows.append(
            PriceRow(
                symbol=str(r[0]).strip(),
                trade_date=actual_date,
                open=_to_decimal(r[4]),
                high=_to_decimal(r[5]),
                low=_to_decimal(r[6]),
                close=_to_decimal(r[2]),
                volume=_to_int(r[8]),
                amount=_to_decimal(r[9]),
                source="tpex",
            )
        )
    return actual_date, rows


# --- chip (三大法人 T86) ---------------------------------------------------

@dataclass(frozen=True)
class ChipRow:
    """三大法人買賣超 — 對應設計 22 §3 chip_data。

    單位: 股 (TWSE/TPEx T86 原生單位)。融資 / 融券餘額不在 T86, 留 None,
    後續 chunk 由其他 endpoint (MI_MARGN / FinMind) 補齊。
    """

    symbol: str
    trade_date: date
    foreign_net_buy: int | None   # 外資合計買賣超 (含外資自營商)
    trust_net_buy: int | None     # 投信買賣超
    dealer_net_buy: int | None    # 自營商買賣超 (自行+避險合計)
    source: str

    def is_valid(self) -> tuple[bool, str | None]:
        """單位股, 但 net_buy 可正可負, 不能像 price 那樣判 <=0。

        防呆: 三個值不能同時為 None (代表整列 parse 失敗)。
        """
        if (
            self.foreign_net_buy is None
            and self.trust_net_buy is None
            and self.dealer_net_buy is None
        ):
            return False, "all three net_buy are null"
        return True, None


def _to_signed_int(s: Any) -> int | None:
    """T86 的買賣超允許負號; 不像 volume 一定 >=0。其餘格式同 _to_int。"""
    if s is None:
        return None
    s = _HTML_TAG_RE.sub("", str(s)).strip().replace(",", "")
    if s in ("", "--", "---", "X"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


@retry(
    retry=retry_if_exception_type((httpx.RequestError, TWSEError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def fetch_twse_chip(trade_date: date) -> tuple[date, list[ChipRow]]:
    """TWSE T86 三大法人買賣超 — 全市場個股。

    schema (19 fields, 2024+):
      0 證券代號 / 1 證券名稱
      2-4   外陸資 (買進/賣出/買賣超)  — 不含外資自營商
      5-7   外資自營商 (買進/賣出/買賣超)
      8-10  投信 (買進/賣出/買賣超)
      11    自營商買賣超 (合計, 自行+避險)
      12-14 自營商自行買賣 (買進/賣出/買賣超)
      15-17 自營商避險 (買進/賣出/買賣超)
      18    三大法人買賣超合計

    輸出口徑:
      foreign_net_buy = field[4] + field[7]   # 外資合計
      trust_net_buy   = field[10]
      dealer_net_buy  = field[11]
    """
    rate_limiter.acquire("twse")
    params = {
        "date": _twse_date_str(trade_date),
        "selectType": "ALLBUT0999",
        "response": "json",
    }
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "finflow-ai/0.1"}) as client:
        resp = client.get(TWSE_T86, params=params)
    if resp.status_code != 200:
        raise TWSEError(f"TWSE T86 HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    stat = body.get("stat")
    if stat != "OK":
        if stat and "沒有符合條件" in stat:
            raise TWSENoDataError(f"TWSE T86 no data for date={trade_date}")
        raise TWSEError(f"TWSE T86 stat={stat} for date={trade_date}")
    actual_date = _parse_response_date(body.get("date"))
    _assert_reasonable_date(trade_date, actual_date)

    rows: list[ChipRow] = []
    for r in body.get("data", []):
        if len(r) < 12:
            continue
        foreign_main = _to_signed_int(r[4])
        foreign_dealer = _to_signed_int(r[7])
        if foreign_main is None and foreign_dealer is None:
            foreign_total: int | None = None
        else:
            foreign_total = (foreign_main or 0) + (foreign_dealer or 0)
        rows.append(
            ChipRow(
                symbol=str(r[0]).strip(),
                trade_date=actual_date,
                foreign_net_buy=foreign_total,
                trust_net_buy=_to_signed_int(r[10]),
                dealer_net_buy=_to_signed_int(r[11]),
                source="twse",
            )
        )
    return actual_date, rows


@retry(
    retry=retry_if_exception_type((httpx.RequestError, TWSEError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def fetch_tpex_chip(trade_date: date) -> tuple[date, list[ChipRow]]:
    """TPEx 3itrade_hedge_result.php — 上櫃三大法人買賣超 (含 ETF / 興櫃以外)。

    Endpoint 支援歷史日期 (與 TPEx daily_close_quotes 不同), d 用民國 YYY/MM/DD。
    se=EW 指上櫃股票市場 (不含創新板/興櫃)。

    schema (24 fields):
      0 代號 / 1 名稱
      2-4   外資不含自營商 (買/賣/買賣超)
      5-7   外資自營商 (買/賣/買賣超)
      8-10  外資合計 (= 2+5, 3+6, 4+7)   — 直接用合計欄
      11-13 投信 (買/賣/買賣超)
      14-16 自營商 (自行買賣)
      17-19 自營商 (避險)
      20-22 自營商合計 (= 14+17 ...)
      23    三大法人合計買賣超
    """
    rate_limiter.acquire("tpex")
    params = {
        "l": "zh-tw",
        "o": "json",
        "se": "EW",
        "t": "D",
        "d": _tpex_date_str(trade_date),
    }
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "finflow-ai/0.1"}) as client:
        resp = client.get(TPEX_T86, params=params)
    if resp.status_code != 200:
        raise TWSEError(f"TPEx T86 HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    stat = body.get("stat")
    if stat not in ("ok", "OK"):
        if isinstance(stat, str) and "沒有符合條件" in stat:
            raise TWSENoDataError(f"TPEx T86 no data for date={trade_date}")
        raise TWSEError(f"TPEx T86 stat={stat} for date={trade_date}")
    actual_date = _parse_response_date(body.get("date"))
    _assert_reasonable_date(trade_date, actual_date)

    tables = body.get("tables") or []
    if not tables:
        return actual_date, []
    main = tables[0]
    rows: list[ChipRow] = []
    for r in main.get("data", []):
        if len(r) < 23:
            continue
        rows.append(
            ChipRow(
                symbol=str(r[0]).strip(),
                trade_date=actual_date,
                foreign_net_buy=_to_signed_int(r[10]),
                trust_net_buy=_to_signed_int(r[13]),
                dealer_net_buy=_to_signed_int(r[22]),
                source="tpex",
            )
        )
    return actual_date, rows


# --- margin / short balance (信用交易餘額) ---------------------------------
#
# TWSE / TPEx 公布的單位是「張」(1 張 = 1000 股)。為了和 chip_data 內既有
# foreign/trust/dealer (股) 欄位單位一致, 入庫前統一 × 1000 轉股。
# 資料公布時間約 17:00, 比 T86 (16:30) 晚 ~30 min, 因此排程拆開到 17:00。

LOT_SHARES = 1000


@dataclass(frozen=True)
class MarginRow:
    """信用交易餘額 — 對應設計 22 §3 chip_data.margin_balance / short_balance。

    margin_balance / short_balance 入庫單位: 股 (TWSE/TPEx 原 endpoint 是張, 已 ×1000)。
    """

    symbol: str
    trade_date: date
    margin_balance: int | None
    short_balance: int | None
    source: str

    def is_valid(self) -> tuple[bool, str | None]:
        """餘額不可為負; 兩欄不可同時 None。"""
        if self.margin_balance is None and self.short_balance is None:
            return False, "both balances null"
        for label, val in (("margin_balance", self.margin_balance), ("short_balance", self.short_balance)):
            if val is not None and val < 0:
                return False, f"{label} < 0 ({val})"
        return True, None


def _lots_to_shares(s: Any) -> int | None:
    v = _to_int(s)
    return v * LOT_SHARES if v is not None else None


@retry(
    retry=retry_if_exception_type((httpx.RequestError, TWSEError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def fetch_twse_margin(trade_date: date) -> tuple[date, list[MarginRow]]:
    """TWSE MI_MARGN — 上市融資融券彙總。

    回 2 張 table:
      table[0] 信用交易統計 (整體市場 3 row)
      table[1] 融資融券彙總 (個股 ~1300 row)  ← 這支 task 用

    table[1] schema (16 fields):
      0 代號 / 1 名稱
      2-7  融資: 買進/賣出/現金償還/前日餘額/今日餘額/次一限額
      8-13 融券: 買進/賣出/現券償還/前日餘額/今日餘額/次一限額
      14   資券互抵 / 15 註記

    輸出:
      margin_balance = field[6]  (今日資餘額, 張 → ×1000 股)
      short_balance  = field[12] (今日券餘額, 張 → ×1000 股)
    """
    rate_limiter.acquire("twse")
    params = {
        "date": _twse_date_str(trade_date),
        "selectType": "ALL",
        "response": "json",
    }
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "finflow-ai/0.1"}) as client:
        resp = client.get(TWSE_MI_MARGN, params=params)
    if resp.status_code != 200:
        raise TWSEError(f"TWSE MI_MARGN HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    stat = body.get("stat")
    if stat != "OK":
        # 信用交易餘額公布晚 (~17:30+), 該日無資料時 stat 含「沒有符合條件」/「無資料」
        if isinstance(stat, str) and ("沒有符合" in stat or "無資料" in stat):
            raise TWSENoDataError(f"TWSE MI_MARGN no data yet for date={trade_date} (stat={stat})")
        raise TWSEError(f"TWSE MI_MARGN stat={stat} for date={trade_date}")
    actual_date = _parse_response_date(body.get("date"))
    _assert_reasonable_date(trade_date, actual_date)

    tables = body.get("tables") or []
    # 個股表是 fields 含 '代號' 那張 (通常 table[1])
    main = next(
        (
            t
            for t in tables
            if t.get("fields") and len(t["fields"]) >= 13 and "代號" in t["fields"][0]
        ),
        None,
    )
    if main is None:
        raise TWSEError("TWSE MI_MARGN 找不到含 '代號' 的個股表")

    rows: list[MarginRow] = []
    for r in main.get("data", []):
        if len(r) < 13:
            continue
        rows.append(
            MarginRow(
                symbol=str(r[0]).strip(),
                trade_date=actual_date,
                margin_balance=_lots_to_shares(r[6]),
                short_balance=_lots_to_shares(r[12]),
                source="twse",
            )
        )
    return actual_date, rows


@retry(
    retry=retry_if_exception_type((httpx.RequestError, TWSEError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def fetch_tpex_margin(trade_date: date) -> tuple[date, list[MarginRow]]:
    """TPEx margin_bal_result.php — 上櫃融資融券餘額。支援歷史日期。

    schema (20 fields):
      0 代號 / 1 名稱
      2 前資餘額(張) / 3 資買 / 4 資賣 / 5 現償 / 6 資餘額(張) / ...
      10 前券餘額(張) / 11 券賣 / 12 券買 / 13 券償 / 14 券餘額(張) / ...

    輸出:
      margin_balance = field[6]  (資餘額, 張 → ×1000 股)
      short_balance  = field[14] (券餘額, 張 → ×1000 股)
    """
    rate_limiter.acquire("tpex")
    params = {"l": "zh-tw", "o": "json", "d": _tpex_date_str(trade_date)}
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "finflow-ai/0.1"}) as client:
        resp = client.get(TPEX_MARGIN, params=params)
    if resp.status_code != 200:
        raise TWSEError(f"TPEx margin HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    stat = body.get("stat")
    if stat not in ("ok", "OK"):
        if isinstance(stat, str) and ("沒有符合" in stat or "無資料" in stat):
            raise TWSENoDataError(f"TPEx margin no data yet for date={trade_date} (stat={stat})")
        raise TWSEError(f"TPEx margin stat={stat} for date={trade_date}")
    actual_date = _parse_response_date(body.get("date"))
    _assert_reasonable_date(trade_date, actual_date)

    tables = body.get("tables") or []
    if not tables or not (tables[0].get("data") or []):
        # TPEx margin 對未公布日期會 stat=ok 但 0 row, 不像 TWSE 回明確錯誤訊息。
        # 統一抛 NoDataError 讓 caller fallback T-1。
        raise TWSENoDataError(f"TPEx margin no data for date={trade_date} (stat=ok but 0 rows)")
    main = tables[0]
    rows: list[MarginRow] = []
    for r in main.get("data", []):
        if len(r) < 15:
            continue
        rows.append(
            MarginRow(
                symbol=str(r[0]).strip(),
                trade_date=actual_date,
                margin_balance=_lots_to_shares(r[6]),
                short_balance=_lots_to_shares(r[14]),
                source="tpex",
            )
        )
    return actual_date, rows
