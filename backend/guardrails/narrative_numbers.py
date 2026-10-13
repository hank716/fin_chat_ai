"""敘事數字驗證（spec 021-narrative-number-guard）。

候選個股的說明文字（thesis / signals / uncertainty）大量轉述當天市場資料的數字，而既有護欄
只驗欄位路徑存不存在，不驗數字對不對。這裡用**確定性規則**把「指標 + 數字」抽出來，
對照 `features.tw.stocks[代號]` 的真值；零 LLM 成本、零對外請求（FR-009）。

設計取捨：**寧可漏報、不可誤報**。認不出指標的數字一律放過（FR-006）——規則式比對的誤判
會直接變成讀者看到的假警示，而漏掉的那句頂多維持現狀。因此每條規則都以「子句」為單位、
帶排除詞，遇到在講大盤/族群/別檔的子句就不比。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

REL_TOLERANCE = 0.05          # FR-003：相對誤差 5%，與顯示精度的四捨五入差取較寬者

_NUM = r"(?P<num>[+\-−－]?\s*\d[\d,]*(?:\.\d+)?)"
_UP = "大漲|上漲|漲幅|反彈|彈升|勁揚|上揚|漲"
_DOWN = "重挫|大跌|下跌|跌幅|暴跌|急跌|回檔|下挫|跌"
_FILL = r"(?:已|僅|約|達|為|亦|雖|仍|高達|只|共|累計|報酬|[\s:：])*"
_SENTENCE_SPLIT = re.compile(r"[。；;\n]")
# 半形逗號夾在數字中間是千分位（58,335），不是子句分隔
_CLAUSE_SPLIT = re.compile(r"[、，（）()]|(?<!\d),|,(?!\d)")
_INSTITUTIONS = (("外資", "foreign"), ("投信", "trust"), ("自營", "dealer"))


@dataclass(frozen=True)
class NumberClaim:
    """文字裡一則「指標 + 數字」的敘述，與資料值比對後的結論。"""

    symbol: str
    metric: str            # features.tw.stocks[symbol] 底下的鍵（fundamentals 以 `fundamentals.x` 表示）
    text: str              # 原文子句
    claimed: float         # 文字宣稱的值（已依方向詞帶正負號）
    actual: float          # 資料值
    ok: bool

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "metric": self.metric, "text": self.text,
                "claimed": self.claimed, "actual": self.actual}


def _to_float(raw: str) -> tuple[float, int, bool]:
    """'−58,335' → (-58335.0, 小數位數, 是否帶明確正負號)。"""
    s = raw.replace(",", "").replace(" ", "").replace("−", "-").replace("－", "-")
    signed = s[:1] in "+-"
    decimals = len(s.split(".")[1]) if "." in s else 0
    return float(s), decimals, signed


def _direction(word: str | None) -> int:
    if not word:
        return 0
    if re.fullmatch(_UP + "|買超|買|連買|增|年增|月增|贏|領先|強於|上|正乖離", word):
        return 1
    return -1


def _lookup(entry: dict[str, Any], metric: str) -> Any:
    node: Any = entry
    for part in metric.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    metric: Callable[[re.Match[str], str | None], str | None]
    # 子句含這些詞就不適用本規則（多半代表數字在講別的對象或別的指標）
    exclude: tuple[str, ...] = ()
    magnitude_only: bool = False   # 指標本身無方向（收盤價、波動度…），只比絕對值
    # 沒有方向詞、正負號或「報酬」字樣就不認領。「高波動（20日15.58%）」被括號切開後只剩
    # 「20日15.58%」，光看子句無從得知它是波動度——2026-09-07 回放實際誤判過。
    needs_cue: bool = False


def _fixed(metric: str) -> Callable[[re.Match[str], str | None], str | None]:
    return lambda _m, _inst: metric


def _period(template: str) -> Callable[[re.Match[str], str | None], str | None]:
    return lambda m, _inst: template.format(m.group("p"))


def _by_institution(template: str) -> Callable[[re.Match[str], str | None], str | None]:
    def pick(m: re.Match[str], inherited: str | None) -> str | None:
        # 取**數字前面最近的**法人：「投信已連賣5日與外資對作」講的是投信，不是句尾的外資。
        before = m.string[:m.start("num")]
        found = [(before.rfind(word), key) for word, key in _INSTITUTIONS if word in before]
        inst = max(found)[1] if found else inherited
        return template.format(inst) if inst else None
    return pick


# 子句在講的不是這檔個股：大盤、族群、海外指數與資產。「工業」「業外資」是「鋼鐵工業外資…」這類族群合計。
_OTHERS = ("大盤", "指數", "族群", "產業", "類股", "平均", "同業", "工業", "業外資", "板塊",
           "費半", "那斯達克", "納斯達克", "標普", "道瓊", "美股", "台股", "加權", "比特幣", "以太",
           "SOX", "NASDAQ", "S&P", "BTC", "ETH", "ADR")
_NOT_INSTITUTIONAL = _OTHERS + ("融資", "融券", "借券")
_RULES: tuple[_Rule, ...] = (
    _Rule(re.compile(r"收盤(?:價)?" + _FILL + _NUM + r"\s*元"), _fixed("close"),
          exclude=_OTHERS, magnitude_only=True),
    # 相對大盤強弱（須帶期間，否則分不出 5 日或 20 日）
    _Rule(re.compile(r"(?:vs_index_|vs\s*大盤\s*)(?P<p>5|20)\s*(?:d|日)" + _FILL + _NUM
                     + r"\s*(?:pp|個百分點|%)"), _period("vs_index_{}d_pct")),
    _Rule(re.compile(r"(?P<p>5|20)\s*日(?:相對大盤|較大盤)?\s*(?P<dir>贏|領先|落後|輸|弱於|強於)?"
                     r"(?:大盤)?" + _FILL + _NUM + r"\s*(?:pp|個百分點)"),
          _period("vs_index_{}d_pct"), exclude=("族群", "產業", "類股", "平均", "同業")),
    # 距均線乖離
    _Rule(re.compile(r"(?:距|站上|跌破|站回|仍在)?\s*(?:MA20|月線)\s*(?P<dir>正乖離|負乖離|上|下)?"
                     + _FILL + r"[（(]?\s*" + _NUM + r"\s*%"), _fixed("dist_ma20_pct"),
          exclude=_OTHERS + ("上方", "下方", "附近", "目標", "止損", "下緣", "上緣")),
    _Rule(re.compile(r"(?:距|站上|跌破|站回|仍在)?\s*(?:MA60|季線)\s*(?P<dir>正乖離|負乖離|上|下)?"
                     + _FILL + r"[（(]?\s*" + _NUM + r"\s*%"), _fixed("dist_ma60_pct"),
          exclude=_OTHERS + ("上方", "下方", "附近", "目標", "止損", "下緣", "上緣")),
    _Rule(re.compile(r"波動度" + _FILL + _NUM + r"\s*%"), _fixed("volatility_20d_pct"),
          exclude=_OTHERS, magnitude_only=True),
    _Rule(re.compile(r"資券比" + _FILL + _NUM + r"\s*%"), _fixed("short_margin_ratio_pct"),
          exclude=_OTHERS, magnitude_only=True),
    _Rule(re.compile(r"(?:成交量|量能|成交量能)(?:放大|增加|擴增)" + _FILL + _NUM + r"\s*倍"),
          _fixed("turnover_surge"), exclude=_OTHERS, magnitude_only=True),
    # 法人買賣超與連續日數（主詞可能在同一句的前一個子句，由 inherited 帶入）
    _Rule(re.compile(r"(?:外資|投信|自營商?)?\s*(?:近)?5\s*日(?:累計|大)?(?P<dir>買超|賣超|買|賣)"
                     + _FILL + _NUM + r"\s*張"), _by_institution("{}_net_buy_5d_lots"),
          exclude=_NOT_INSTITUTIONAL),
    _Rule(re.compile(r"(?:外資|投信|自營商?)?\s*單日(?:同步)?(?P<dir>買超|賣超)" + _FILL + _NUM + r"\s*張"),
          _by_institution("{}_net_buy_1d_lots"), exclude=_NOT_INSTITUTIONAL),
    _Rule(re.compile(r"(?:外資|投信|自營商?)?\s*(?P<dir>連買|連賣)\s*" + _NUM + r"\s*日"),
          _by_institution("{}_net_streak"), exclude=_NOT_INSTITUTIONAL),
    _Rule(re.compile(r"融資(?:近)?5\s*日(?:續|反向|仍|激|再|大)*(?P<dir>增|減)(?:加|少)?" + _FILL + _NUM + r"\s*張"),
          _fixed("margin_chg_5d_lots")),
    # 基本面
    _Rule(re.compile(r"營收[^增減\d]{0,8}?(?P<dir>年增|年減)" + _FILL + _NUM + r"\s*%"),
          _fixed("fundamentals.revenue_yoy_pct"), exclude=_OTHERS),
    _Rule(re.compile(r"(?P<dir>年增|年減)" + _FILL + _NUM + r"\s*%"),
          _fixed("fundamentals.revenue_yoy_pct"), exclude=_OTHERS + ("EPS", "獲利", "盈餘", "毛利", "稅")),
    _Rule(re.compile(r"(?P<dir>月增|月減)" + _FILL + _NUM + r"\s*%"),
          _fixed("fundamentals.revenue_mom_pct"), exclude=_OTHERS + ("EPS", "獲利", "盈餘", "毛利", "稅")),
    _Rule(re.compile(r"毛利率" + _FILL + _NUM + r"\s*%"), _fixed("fundamentals.gross_margin_pct"),
          exclude=_OTHERS),
    _Rule(re.compile(r"(?:營益率|營業利益率)" + _FILL + _NUM + r"\s*%"),
          _fixed("fundamentals.operating_margin_pct"), exclude=_OTHERS),
    _Rule(re.compile(r"負債比(?:率)?" + _FILL + _NUM + r"\s*%"), _fixed("fundamentals.debt_ratio_pct"),
          exclude=_OTHERS, magnitude_only=True),
    _Rule(re.compile(r"(?:(?:近四季|近4季)\s*EPS|EPS_TTM)" + _FILL + _NUM + r"\s*元"),
          _fixed("fundamentals.eps_ttm"), exclude=_OTHERS),
    _Rule(re.compile(r"單季\s*EPS" + _FILL + _NUM + r"\s*元"), _fixed("fundamentals.eps_quarter"),
          exclude=_OTHERS),
    # 報酬：放最後，並排除所有已被上面規則認領的語境
    _Rule(re.compile(r"單日(?:股價)?(?:已|雖|仍)?\s*(?P<dir>" + _UP + "|" + _DOWN + r")?" + _FILL + _NUM + r"\s*%"),
          _fixed("return_1d_pct"), exclude=_OTHERS + ("買超", "賣超"), needs_cue=True),
    _Rule(re.compile(r"(?P<p>5|20)\s*日(?:報酬|累計|股價)?\s*(?P<dir>" + _UP + "|" + _DOWN + r")?"
                     + _FILL + _NUM + r"\s*%"), _period("return_{}d_pct"),
          exclude=_OTHERS + ("波動", "買超", "賣超", "融資", "融券", "月線", "季線", "MA", "相關", "乖離", "vs"),
          needs_cue=True),
)


def _within_tolerance(claimed: float, actual: float, decimals: int) -> bool:
    return abs(claimed - actual) <= max(REL_TOLERANCE * abs(actual), 0.5 * 10 ** (-decimals)) + 1e-9


def _check(rule: _Rule, m: re.Match[str], entry: dict[str, Any], inherited: str | None,
           symbol: str, clause: str) -> NumberClaim | None:
    metric = rule.metric(m, inherited)
    if not metric:
        return None
    actual = _lookup(entry, metric)
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        return None                                   # 資料值為空 → 不核對（FR-006）
    value, decimals, signed = _to_float(m.group("num"))
    direction = _direction(m.groupdict().get("dir"))
    if rule.needs_cue and not (direction or signed or "報酬" in clause):
        return None
    if rule.magnitude_only:
        claimed, truth = abs(value), abs(float(actual))
    elif direction and not signed:
        claimed, truth = direction * abs(value), float(actual)
    elif direction and signed:
        # 「跌破月線達-24.56%」：方向詞與符號並存時以符號為準，方向詞只是語氣
        claimed, truth = value, float(actual)
    elif signed:
        claimed, truth = value, float(actual)
    else:
        # 沒有任何方向資訊（「5日報酬0%」）：只比大小，不猜正負
        claimed, truth = abs(value), abs(float(actual))
    return NumberClaim(symbol=symbol, metric=metric, text=clause.strip(), claimed=claimed,
                       actual=float(actual), ok=_within_tolerance(claimed, truth, decimals))


def extract_claims(symbol: str, text: str | None, entry: dict[str, Any]) -> list[NumberClaim]:
    """一段文字裡所有認得出指標的數字敘述。認不出的不回傳。"""
    claims: list[NumberClaim] = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        inherited: str | None = None
        for clause in _CLAUSE_SPLIT.split(sentence):
            if not clause.strip():
                continue
            if any(word in clause for word in _NOT_INSTITUTIONAL):
                inherited = None                      # 主詞換成族群/散戶籌碼，後續子句不得沿用前面的法人
            for rule in _RULES:
                if any(word in clause for word in rule.exclude):
                    continue
                m = rule.pattern.search(clause)
                if not m:
                    continue
                claim = _check(rule, m, entry, inherited, symbol, clause)
                if claim:
                    claims.append(claim)
                break                                 # 一個子句只認領一次，避免同一數字被兩條規則重複計
            # 這個子句點名的最後一個法人，留給下一個省略主詞的子句（「外資連買7日、5日買超…」）
            named = [(clause.rfind(word), key) for word, key in _INSTITUTIONS if word in clause]
            if named and not any(word in clause for word in _NOT_INSTITUTIONAL):
                inherited = max(named)[1]
    return claims


def check_item(item: Any, entry: dict[str, Any]) -> list[NumberClaim]:
    """一檔候選（WatchItem 或 dict）的全部數字敘述。"""
    get = item.get if isinstance(item, dict) else lambda k, d=None: getattr(item, k, d)
    symbol = str(get("symbol") or "")
    texts = [get("thesis"), *(get("signals") or []), get("uncertainty")]
    out: list[NumberClaim] = []
    seen: set[tuple[str, float]] = set()
    for text in texts:
        for claim in extract_claims(symbol, text, entry):
            key = (claim.metric, claim.claimed)
            if key in seen:                           # thesis 與 signals 常重複同一個數字，只計一次
                continue
            seen.add(key)
            out.append(claim)
    return out
