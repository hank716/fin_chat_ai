"""查證結局分類與跨報告彙總（spec 023-verification-efficacy，US1）。

## 為什麼要有這一層

`fact_checks[].verdict` 是**模型自述**。`unverifiable` 這一個值同時涵蓋三種完全不同的情況：

1. 開了原文，但原文確實不支持該敘述 ← 有價值的查證結果
2. 想開但**額度用盡**，根本沒查 ← 偽裝成查證結果
3. 嘗試開但**連結打不開** ← 偽裝成查證結果

分不出來就沒辦法調參（憲章 II 要求查證額度的調整必須有遙測依據），也沒辦法對讀者誠實。
這裡把「模型說了什麼」（verdict）與「工具做了什麼」（`FetchAttempt`）交叉比對，
輸出**由系統推導**的結局分類（FR-006）。

## 為什麼放在 reports 而不是 ai

`claude_client` 是供應商轉接層，只該回報「工具發生了什麼」；「這則線索算不算查證成功」
是業務判斷。分類規則會隨產品定義改，轉接層不該跟著動。

## CLI

    docker compose exec -T backend python -m reports.verification_stats

跨報告彙總各結局次數、各來源網域的成功率與失敗原因分布。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

# ── 結局分類（plan.md 的表，逐格對應）──────────────────────────────────
CONFIRMED = "confirmed"
CONTRADICTED = "contradicted"
CHECKED_INSUFFICIENT = "checked_insufficient"    # 開了原文，但原文不足以判定
UNCHECKED_BUDGET = "unchecked_budget"            # 額度用盡，根本沒開
UNCHECKED_UNREACHABLE = "unchecked_unreachable"  # 開了但這個來源打不開
UNCHECKED_TRANSIENT = "unchecked_transient"      # 上游暫時性失敗
UNCHECKED_OTHER = "unchecked_other"              # 其餘（含模型自己放棄查證）

OUTCOMES = (
    CONFIRMED, CONTRADICTED, CHECKED_INSUFFICIENT,
    UNCHECKED_BUDGET, UNCHECKED_UNREACHABLE, UNCHECKED_TRANSIENT, UNCHECKED_OTHER,
)
# 「已核對」＝實際開過原文，判定有依據。這三類才可以被當成查證結果閱讀。
CHECKED = frozenset({CONFIRMED, CONTRADICTED, CHECKED_INSUFFICIENT})
# 「裁決成功」沿用 8/12 盤點的口徑（confirmed + contradicted），便於前後對照。
ADJUDICATED = frozenset({CONFIRMED, CONTRADICTED})

# error_code → 結局。值域來自 anthropic SDK 的 WebFetchToolResultErrorCode Literal，
# 已於容器內（0.120.2）逐字核對。SDK 新增值會落到 UNCHECKED_OTHER，不會靜默錯分。
_BUDGET_CODES = frozenset({"max_uses_exceeded"})
_UNREACHABLE_CODES = frozenset({
    "url_not_accessible", "url_not_allowed", "unsupported_content_type", "url_too_long",
})
_TRANSIENT_CODES = frozenset({"too_many_requests", "unavailable"})


def _host_of(netloc: str) -> str:
    """netloc → 比對用的 host：轉小寫並去掉 `www.`。

    ⚠️ **`www.` 必須在這裡就去掉**：召回層拿到的是 `https://www.cnyes.com/...`，模型
    送給 `web_fetch` 的常是 `https://cnyes.com/...`（反之亦然）。只差一個 `www.` 就配不上，
    結果是一則**真的查證成功**的線索被判成 `claimed_unbacked`（「模型宣稱查了但沒紀錄」），
    然後被 guardrail 從 news_digest / sources 整條刪掉——誤刪正確內容比漏報更難察覺。
    """
    return netloc.lower().removeprefix("www.")


def normalize_url(url: str | None) -> str:
    """比對用的正規化：去 fragment、去尾斜線、host 轉小寫並去 `www.`。

    召回層寫進 facts pack 的 URL 與模型送給 `web_fetch` 的 URL 常有這類無意義差異，
    逐字比對會把「查過的」誤判成「沒查的」——那正是本模組要消除的錯誤。

    ⚠️ **query 一律保留**：它常常就是文章 id（`/article?id=1` 與 `/article?id=2` 是兩篇
    不同的報導）。把 query 當雜訊丟掉會讓「沒查的那則」冒名頂替「查過的那則」。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.netloc:
        return raw.rstrip("/").lower()
    path = parts.path.rstrip("/")
    query = "?" + parts.query if parts.query else ""
    return parts.scheme.lower() + "://" + _host_of(parts.netloc) + path + query


def _loose_key(url: str) -> str:
    """退一步的比對鍵：netloc + path（忽略 scheme 與 query）。

    只在**這個鍵唯一對應一個 URL** 時才可以拿來配對，見 `_unambiguous()`。忽略 query
    是為了吸收 utm 之類的追蹤參數，但同一個 path 底下若有多個不同 query 的 URL，
    這個鍵就分不出誰是誰了。
    """
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    return _host_of(parts.netloc) + parts.path.rstrip("/")


def _unambiguous(keys: Iterable[str]) -> set[str]:
    """只出現一次的鍵。出現兩次以上者不得用於寬鬆配對——寧可判成「沒查」也不能張冠李戴。"""
    counts = Counter(keys)
    return {k for k, n in counts.items() if n == 1}


def domain_of(url: str | None) -> str:
    """來源網域（FR-003：足以做網域層級彙總）。"""
    return _host_of(urlsplit((url or "").strip()).netloc)


@dataclass(frozen=True)
class ClueOutcome:
    """一則線索的查證結局。每則線索落入且僅落入一類。"""

    url: str
    domain: str
    claim: str                   # 線索原文（guardrail 比對引用時用，不落地到報告 JSON）
    outcome: str
    verdict: str | None          # 模型自述（可能為 None＝該則根本沒被裁決）
    error_code: str | None       # 工具失敗原因（成功或無 attempt 時為 None）
    attempted: bool              # 有沒有實際發出過 fetch
    claimed_unbacked: bool       # 模型宣稱已查證，但沒有任何成功的 fetch 支撐
    from_facts: bool             # False＝這則 fact_check 對不上任何召回線索

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "domain": self.domain, "outcome": self.outcome,
            "verdict": self.verdict, "error_code": self.error_code,
            "attempted": self.attempted, "claimed_unbacked": self.claimed_unbacked,
            "from_facts": self.from_facts,
        }


def _attr(obj: Any, name: str) -> Any:
    """attempt 可能是 dataclass（線上路徑）或 dict（讀報告 JSON 回放）。"""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _index_attempts(attempts: Iterable[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """URL → 最具代表性的 attempt（成功優先），另回一份寬鬆鍵索引。

    ⚠️ **依 URL 去重**：實測發生過同一個來源頁支撐多則裁決（8/20 的 cnyes），
    不去重會讓額度計算與成功率統計重複計次（spec Edge Case）。
    """
    exact: dict[str, Any] = {}
    loose: dict[str, Any] = {}
    for att in attempts or []:
        url = normalize_url(_attr(att, "url"))
        if not url:
            continue
        ok = bool(_attr(att, "ok"))
        for index, key in ((exact, url), (loose, _loose_key(url))):
            prev = index.get(key)
            # 成功壓過失敗：同一個 URL 開過兩次、只要一次成功就算查過了。
            if prev is None or (ok and not bool(_attr(prev, "ok"))):
                index[key] = att
    # 兩個**不同**的 URL 撞進同一個寬鬆鍵時（同 path 不同 query），這個鍵已經無法指認
    # 是誰被開過了——留著只會讓沒查的那則繼承別人的成功紀錄。整個鍵丟掉，退回 exact。
    for key in set(loose) - _unambiguous(_loose_key(u) for u in exact):
        loose.pop(key, None)
    return exact, loose


def _pick_verdict(verdicts: list[str]) -> str | None:
    """同一 URL 有多則裁決時，取資訊量最高的一則（有判定 > unverifiable）。"""
    for want in (CONFIRMED, CONTRADICTED):
        if want in verdicts:
            return want
    return verdicts[0] if verdicts else None


def _failure_outcome(
    error_code: str | None, *, attempted: bool, budget_exhausted: bool,
) -> str:
    if attempted:
        if error_code in _BUDGET_CODES:
            return UNCHECKED_BUDGET
        if error_code in _UNREACHABLE_CODES:
            return UNCHECKED_UNREACHABLE
        if error_code in _TRANSIENT_CODES:
            return UNCHECKED_TRANSIENT
        return UNCHECKED_OTHER
    # 完全沒有 attempt 的兩種分流（FR-001）：額度已耗盡＝系統沒能力查；
    # 額度還有＝模型**主動放棄**查證。混在一起會讓 US3 得出錯誤結論。
    return UNCHECKED_BUDGET if budget_exhausted else UNCHECKED_OTHER


def _clue_fields(item: Any) -> tuple[str, str]:
    """接受 FactEvent 物件 / dict / 純 URL 字串，回 (url, claim)。

    呼叫端有兩種：線上路徑給的是 `retrieval.FactEvent`，測試與回放給的是 dict 或字串。
    在這裡收斂，好過讓每個呼叫端各自轉一次。
    """
    if isinstance(item, str):
        return item, ""
    if isinstance(item, dict):
        return str(item.get("url") or ""), str(item.get("claim") or "")
    return str(getattr(item, "url", "") or ""), str(getattr(item, "claim", "") or "")


def classify_outcomes(
    clues: Iterable[Any],
    fact_checks: Iterable[dict[str, Any]],
    attempts: Iterable[Any],
    *,
    fetch_limit: int,
    fetch_requests: int,
) -> list[ClueOutcome]:
    """把「模型說了什麼」與「工具做了什麼」交叉比對成結局分類（FR-001、FR-002、FR-006）。

    `clues` 是召回層線索（FactEvent / dict / URL 字串皆可）；對不上任何線索的 fact_check 仍會被列出
    （`from_facts=False`）——那代表模型裁決了一個召回層沒給過的來源，本身就是訊號。
    """
    exact, loose = _index_attempts(attempts)
    budget_exhausted = fetch_limit > 0 and fetch_requests >= fetch_limit

    checks: dict[str, list[str]] = defaultdict(list)
    for check in fact_checks or []:
        url = normalize_url(check.get("url"))
        if url:
            checks[url].append(str(check.get("verdict") or ""))

    ordered: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for item in clues or []:
        raw_url, claim = _clue_fields(item)
        url = normalize_url(raw_url)
        if url and url not in seen:
            seen.add(url)
            ordered.append((url, claim, True))
    for url in checks:
        if url not in seen:
            seen.add(url)
            ordered.append((url, "", False))

    # 線索端同樣要防張冠李戴：兩則不同線索共用一個寬鬆鍵時，就算 attempts 那邊只有一筆
    # 紀錄，也無法斷定它屬於哪一則。此時一律不用寬鬆配對（fail-closed）。
    safe_loose = _unambiguous(_loose_key(u) for u, _claim, _src in ordered)

    outcomes: list[ClueOutcome] = []
    for url, claim, from_facts in ordered:
        att = exact.get(url)
        if att is None and _loose_key(url) in safe_loose:
            att = loose.get(_loose_key(url))
        attempted = att is not None
        ok = bool(_attr(att, "ok")) if attempted else False
        error_code = None if ok else (_attr(att, "error_code") if attempted else None)
        verdict = _pick_verdict([v for v in checks.get(url, []) if v])

        if ok and verdict in ADJUDICATED:
            outcome = str(verdict)
        elif ok:
            # 開了原文卻判 unverifiable（或根本沒回裁決）＝有效的查證結果，只是原文不足。
            outcome = CHECKED_INSUFFICIENT
        else:
            outcome = _failure_outcome(
                error_code, attempted=attempted, budget_exhausted=budget_exhausted,
            )

        outcomes.append(ClueOutcome(
            url=url,
            domain=domain_of(url),
            claim=claim,
            outcome=outcome,
            verdict=verdict,
            error_code=str(error_code) if error_code else None,
            attempted=attempted,
            # 模型宣稱查證成功，卻沒有任何成功的 fetch 支撐。8/20 實際發生過
            # （4 則 confirmed 分屬 4 個 URL，當天只開了 3 次），這是 FR-006 的核心風險。
            claimed_unbacked=(verdict in ADJUDICATED and not ok),
            from_facts=from_facts,
        ))
    return outcomes


def summarize(outcomes: Iterable[ClueOutcome]) -> dict[str, int]:
    """結局 → 次數。缺席的分類不補 0，讓報告 JSON 保持精簡。"""
    return dict(Counter(o.outcome for o in outcomes))


def count_of(outcomes: Iterable[ClueOutcome], outcome: str) -> int:
    return sum(1 for o in outcomes if o.outcome == outcome)


# ── 跨報告彙總（T012/T013、FR-005、SC-002）────────────────────────────

# 8/07 及更早的報告受 grounding 轉址 bug 影響（web_fetch 對轉址網域一律回 url_not_allowed），
# 那期間的 0% 成功率不是來源品質問題。混進來會污染基準（spec Assumptions 明文要求排除）。
REDIRECT_BUG_CUTOFF = "20260807"


def _report_date(path: Path) -> str:
    """morning_YYYYMMDD_HHMMSS.json → YYYYMMDD。取不到就回空字串（不猜）。"""
    parts = path.stem.split("_")
    return parts[1] if len(parts) > 2 and parts[1].isdigit() else ""


def _eligible(report: dict[str, Any]) -> tuple[bool, str]:
    """回 (是否納入統計, 排除理由)。

    降級供應商與節儉模式都**不做查證**——它們的 0 次 fetch 是預期行為，不是失敗，
    計進來會把成功率的分母灌大（spec Edge Cases）。
    """
    cost = report.get("cost") or {}
    if cost.get("frugal_mode"):
        return False, "frugal"
    if str(cost.get("decision_provider") or "") != "anthropic":
        return False, "fallback"
    return True, ""


def _ratio(numerator: int, denominator: int) -> float | None:
    """分母為 0 時回 None 而不是 0.0——「今天沒有線索」與「今天全軍覆沒」是兩件事
    （spec Edge Case：召回 0 則時分母為 0，不得當機或顯示 0%）。"""
    return round(numerator / denominator, 4) if denominator else None


def default_reports_dir() -> Path:
    """報告落地目錄。容器內是 `/data/reports`（LOCAL_STORAGE_PATH），主機上跑才是相對路徑。

    寫死 "storage/reports" 會在容器內指到 `storage` 這個 **Python 套件**目錄，
    glob 不到任何檔案卻也不報錯——只會印出一張空表，看起來像「沒有失敗案例」。
    """
    try:
        from config import settings
        return Path(settings.local_storage_path) / "reports"
    except Exception:  # noqa: BLE001 — 沒有 config 的情境（主機上直接跑）退回相對路徑
        return Path("storage/reports")


def aggregate(reports_dir: str | Path | None = None) -> dict[str, Any]:
    """掃過所有報告 JSON，回可直接回答 SC-002 的彙總。

    舊報告（沒有 `clues` 欄位）**不混進分母**——那會讓「未查證」看起來憑空變少。
    另外走 legacy 統計，只用當時就存在的欄位（facts_n / fetch_requests / verdicts）。
    """
    modern_days = 0
    outcome_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    domain_stats: dict[str, Counter[str]] = defaultdict(Counter)
    claimed_unbacked = 0
    legacy_verdicts: Counter[str] = Counter()
    legacy = {"days": 0, "facts": 0, "fact_checks": 0, "fetches": 0,
              "adjudicated": 0, "capped_days": 0}
    skipped: Counter[str] = Counter()

    root = Path(reports_dir) if reports_dir is not None else default_reports_dir()
    for path in sorted(root.glob("morning_*.json")):
        date = _report_date(path)
        if date and date <= REDIRECT_BUG_CUTOFF:
            skipped["redirect_bug"] += 1
            continue
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped["unreadable"] += 1
            continue
        ok, why = _eligible(report)
        if not ok:
            skipped[why] += 1
            continue
        verification = (report.get("cost") or {}).get("verification") or {}
        if not verification:
            skipped["no_verification"] += 1
            continue

        clues = verification.get("clues")
        if clues:
            modern_days += 1
            for clue in clues:
                outcome = str(clue.get("outcome") or UNCHECKED_OTHER)
                outcome_counts[outcome] += 1
                if clue.get("claimed_unbacked"):
                    claimed_unbacked += 1
                code = clue.get("error_code")
                if code:
                    error_counts[str(code)] += 1
                domain = str(clue.get("domain") or "")
                if domain:
                    domain_stats[domain]["ok" if outcome in CHECKED else "fail"] += 1
                    if code:
                        domain_stats[domain]["code:" + str(code)] += 1
            continue

        legacy["days"] += 1
        legacy["facts"] += int(verification.get("facts_n") or 0)
        legacy["fact_checks"] += int(verification.get("fact_checks_n") or 0)
        fetches = int(verification.get("fetch_requests") or 0)
        legacy["fetches"] += fetches
        limit = int(verification.get("fetch_limit") or 0)
        if limit and fetches >= limit:
            legacy["capped_days"] += 1
        for verdict, count in (verification.get("verdicts") or {}).items():
            legacy_verdicts[str(verdict)] += int(count)
            if verdict in ADJUDICATED:
                legacy["adjudicated"] += int(count)

    total = sum(outcome_counts.values())
    checked_n = sum(v for k, v in outcome_counts.items() if k in CHECKED)
    adjudicated_n = sum(v for k, v in outcome_counts.items() if k in ADJUDICATED)
    failures = total - checked_n
    accounted = outcome_counts[UNCHECKED_BUDGET] + outcome_counts[UNCHECKED_UNREACHABLE]
    return {
        "days": modern_days,
        "clues": total,
        "outcomes": dict(outcome_counts),
        "checked_n": checked_n,
        "adjudicated_n": adjudicated_n,
        "adjudication_rate": _ratio(adjudicated_n, total),
        "failures_n": failures,
        "budget_share": _ratio(outcome_counts[UNCHECKED_BUDGET], failures),
        "unreachable_share": _ratio(outcome_counts[UNCHECKED_UNREACHABLE], failures),
        # SC-002：兩者相加須涵蓋 95% 以上的失敗案例
        "sc002_coverage": _ratio(accounted, failures),
        "claimed_unbacked_n": claimed_unbacked,
        "error_codes": dict(error_counts),
        "domains": {d: dict(c) for d, c in sorted(domain_stats.items())},
        "legacy": {**legacy, "verdicts": dict(legacy_verdicts),
                   "adjudication_rate": _ratio(legacy["adjudicated"],
                                               legacy["fact_checks"])},
        "skipped": dict(skipped),
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else "{:.0f}%".format(value * 100)


def render(summary: dict[str, Any]) -> str:
    """人可讀的彙總表（CLI 用）。"""
    lines: list[str] = ["=== 查證結局彙總（spec 023 US1）==="]
    lines.append("新格式報告：{} 篇 / 線索 {} 則".format(summary["days"], summary["clues"]))
    if summary["clues"]:
        for outcome in OUTCOMES:
            count = summary["outcomes"].get(outcome, 0)
            mark = "[已核對]" if outcome in CHECKED else "[未查證]"
            share = _pct(_ratio(count, summary["clues"]))
            lines.append("  {} {:<22} {:>4}  {}".format(mark, outcome, count, share))
        lines.append("  裁決成功率（confirmed+contradicted）：{}".format(
            _pct(summary["adjudication_rate"])))
        lines.append(
            "  失敗歸因：額度不足 {} / 來源打不開 {} → SC-002 涵蓋率 {}（門檻 95%）".format(
                _pct(summary["budget_share"]), _pct(summary["unreachable_share"]),
                _pct(summary["sc002_coverage"]))
        )
        if summary["claimed_unbacked_n"]:
            lines.append("  [警告] 模型宣稱已查證但無成功 fetch 支撐：{} 則".format(
                summary["claimed_unbacked_n"]))
        if summary["error_codes"]:
            codes = ", ".join(
                "{} x{}".format(k, v) for k, v in
                sorted(summary["error_codes"].items(), key=lambda kv: -kv[1])
            )
            lines.append("  error_code 分布：" + codes)
        if summary["domains"]:
            lines.append("--- 各來源網域 ---")
            ranked = sorted(
                summary["domains"].items(),
                key=lambda kv: -sum(v for k, v in kv[1].items()
                                    if not k.startswith("code:")),
            )
            for domain, stats in ranked:
                codes = ", ".join("{} x{}".format(k[5:], v) for k, v in stats.items()
                                  if k.startswith("code:"))
                lines.append("  {:<34} 成功 {:>3} / 失敗 {:>3}{}".format(
                    domain, stats.get("ok", 0), stats.get("fail", 0),
                    "  [" + codes + "]" if codes else ""))
    legacy = summary["legacy"]
    if legacy["days"]:
        lines.append("--- 舊格式（僅粗略欄位，不併入上表分母）---")
        lines.append(
            "  {} 篇 / 線索 {} 則 / 裁決 {} 則 / 實際 fetch {} 次".format(
                legacy["days"], legacy["facts"], legacy["fact_checks"], legacy["fetches"])
        )
        structural = max(legacy["facts"] - legacy["fetches"], 0)
        lines.append(
            "  結構性未查證（線索數 - fetch 次數）：{} 則 {} "
            "← 這批連被開啟的機會都沒有，與來源品質無關".format(
                structural, _pct(_ratio(structural, legacy["facts"])))
        )
        lines.append("  裁決成功率：{}  {}".format(
            _pct(legacy["adjudication_rate"]), legacy["verdicts"]))
    if summary["skipped"]:
        lines.append(
            "排除：{}（redirect_bug=轉址 bug 期、fallback=降級供應商、frugal=節儉模式）"
            .format(summary["skipped"])
        )
    return "\n".join(lines)


def main() -> None:
    import sys

    root = sys.argv[1] if len(sys.argv) > 1 else None
    print(render(aggregate(root)))


if __name__ == "__main__":
    main()
