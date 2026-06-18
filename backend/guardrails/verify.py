"""Verification Guardrail（M5，對齊 design_docs §18）。

檢查 Gemini 產出的 BriefResult 是否超出 features 資料範圍、是否引用不存在的數據/標的/新聞、
是否使用禁語（買賣建議 / 必然因果）。對「捏造類」違規直接移除該片段並記錄；對「禁語類」
標記警示。回傳清理後的 result + guardrail 報告（存進 report JSON、頁面顯示攔截狀態）。

六道 guard：Source(Metric) / Symbol / News Citation / Advice / Intermarket Causality / Data Age。
另做新聞分層強制：social 來源比對後標 tier，頁面以情緒訊號呈現。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from ai.schemas import BriefResult

_MISSING = object()

# 過度承諾/保證語禁令（仍禁）。註：依使用者決策，本工具改為「可給方向/目標價/止損價」
# 的輔助角色，故不再攔「建議買進/賣出」等買賣指令；只擋誇大保證與包賺無風險類字眼。
ADVICE_BANNED = (
    "保證獲利", "一定會漲", "一定會跌", "穩賺", "包賺", "必賺", "無風險", "穩賺不賠",
)
CAUSALITY_BANNED = (
    "一定造成", "必然導致", "保證影響", "隔日一定", "必漲", "必跌",
    "一定上漲", "一定下跌", "必然上漲", "必然下跌",
)


# path 段：base key（含 CJK/數字/底線等，但不含括號）後可接零或多個 bracket 存取段
# （[0] 數字索引 或 ["代碼"]/['名稱'] 字串鍵），如 stocks["2408"]、top_foreign_buy_5d[0]。
_PATH_PART_RE = re.compile(r"^([^\[\].]*)((?:\[[^\[\]]*\])*)$")
_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
# 註解起始符：模型常把說明（如「（是…）=1.38%）」）直接黏在 source_ref 後面，截斷後再解析。
_ANNOT_RE = re.compile(r"[（(=\s]")


def _bracket_accessor(raw: str) -> tuple[str, Any]:
    """把 bracket 內容轉成存取子：('idx', int) 數字索引 或 ('key', str) 字串鍵。"""
    s = raw.strip()
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        return "key", s[1:-1]                  # 去引號 → 字串鍵（如 "2408"、"航運業"、"SOX~NASDAQ"）
    if s.lstrip("-").isdigit():
        return "idx", int(s)                   # 純數字 → 清單索引
    return "key", s                            # 裸字串 → 也當字串鍵


def _resolve(features: dict[str, Any], ref: str) -> Any:
    """解析 source_ref 到 features 內的值，容忍模型實際用的 JSONPath 方言。

    支援：dict key、數字清單索引、**字串鍵 bracket**（["2408"]/["航運業"]），並寬容：
      ① 根前綴 `features`／`$`（JSONPath）皆可省略；
      ② source_ref 後黏的中文註解/`=值`（模型常見）自動截斷後再解析。
    例：features.tw.index.return_20d_pct、$.tw.stocks["2408"].close、
        $.us_crypto.return_correlations_20d["SOX~NASDAQ"]
    僅放寬「語法」，仍嚴格要求解析到的欄位真實存在於 features（捏造路徑照樣回 _MISSING 被移除）。
    """
    if not ref:
        return _MISSING
    cut = _ANNOT_RE.search(ref)                # 截掉黏在後面的註解（首個 （ ( = 或空白起）
    ref = (ref[: cut.start()] if cut else ref).strip()
    parts = ref.split(".")
    if parts and parts[0] in ("features", "$", ""):
        parts = parts[1:]
    cur: Any = features
    for p in parts:
        m = _PATH_PART_RE.match(p)
        if not m:
            return _MISSING
        key, idx_str = m.group(1), m.group(2)
        if key:                                # 段首可能直接是 bracket（key 為空）則跳過 key 解析
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                return _MISSING
        for raw in _BRACKET_RE.findall(idx_str):
            kind, val = _bracket_accessor(raw)
            if kind == "idx" and isinstance(cur, (list, tuple)) and 0 <= val < len(cur):
                cur = cur[val]
            elif kind == "key" and isinstance(cur, dict) and val in cur:
                cur = cur[val]
            else:
                return _MISSING
    return cur


def _scan_phrases(text: str | None, banned: tuple[str, ...]) -> list[str]:
    if not text:
        return []
    return [w for w in banned if w in text]


def run_guardrails(result: BriefResult, features: dict[str, Any]) -> tuple[BriefResult, dict]:
    cleaned = result.model_copy(deep=True)
    violations: list[dict[str, str]] = []
    counts = {
        "evidence_checked": 0, "evidence_dropped": 0,
        "news_checked": 0, "news_dropped": 0,
        "symbols_dropped": 0, "phrase_warnings": 0,
    }

    def add(guard: str, severity: str, detail: str) -> None:
        violations.append({"guard": guard, "severity": severity, "detail": detail})

    # ── Source / Metric Guard：evidence.source_ref 必須存在於 features ──
    for sec in cleaned.sections:
        kept = []
        for ev in sec.evidence:
            if ev.source_ref:
                counts["evidence_checked"] += 1
                if _resolve(features, ev.source_ref) is _MISSING:
                    counts["evidence_dropped"] += 1
                    add("metric", "error",
                        f"[{sec.title}] 引用不存在的欄位 {ev.source_ref}（{ev.label}={ev.value}）已移除")
                    continue
            kept.append(ev)
        sec.evidence = kept

    # ── Symbol Guard：候選標的必須在 features.tw.stocks ──
    # 必須 fail-closed：若 stocks 為空（上游台股資料失敗），代表「沒有任何可驗證的合法符號」，
    # 此時模型給的候選一律無法比對 → 全部移除並記錄，而非放行（放行 == guard 形同失效）。
    tw_symbols = set((features.get("tw", {}) or {}).get("stocks", {}).keys())
    for attr, label in (("tw_watchlist", "正向候選"), ("tw_caution", "負向候選")):
        kept = []
        for w in getattr(cleaned, attr):
            if w.symbol not in tw_symbols:
                counts["symbols_dropped"] += 1
                why = "資料範圍為空（台股資料疑似缺失）" if not tw_symbols else "不在資料範圍"
                add("symbol", "error", f"{label} {w.symbol} {w.name} {why}，已移除")
                continue
            kept.append(w)
        setattr(cleaned, attr, kept)

    # ── News Citation Guard：須有 source/date/title 且比對得到 features.news（url 可選，缺則回填）──
    news_by_url = {n.get("url"): n for n in features.get("news", []) if n.get("url")}
    news_by_title = {n.get("title"): n for n in features.get("news", [])}
    kept_news = []
    for nd in cleaned.news_digest:
        counts["news_checked"] += 1
        match = news_by_url.get(nd.url) or news_by_title.get(nd.title)
        if match is None:
            counts["news_dropped"] += 1
            add("news", "error", f"新聞「{nd.title[:30]}」無法比對到來源資料，疑似捏造，已移除")
            continue
        # url 為可選（schema 允許 None）：已比對到真實來源即非捏造，不因缺 url 而丟。
        if not (nd.source and nd.date and nd.title):
            counts["news_dropped"] += 1
            add("news", "error", f"新聞「{nd.title[:30]}」缺 source/date/title，已移除")
            continue
        if not nd.url:                                # 模型沒帶 url 時用比對到的來源回填，頁面才有連結
            nd.url = match.get("url")
        nd.tier = match.get("tier", "authoritative")  # 由原始資料回填分層
        kept_news.append(nd)
    cleaned.news_digest = kept_news

    # ── Advice / Intermarket Causality Guard：掃描所有敘事文字禁語 ──
    texts: list[tuple[str, str]] = [("簡短結論", cleaned.headline)]
    for sec in cleaned.sections:
        texts.append((sec.title, sec.narrative))
    for w in cleaned.tw_watchlist + cleaned.tw_caution:
        texts.append((f"候選 {w.symbol}", w.thesis))
    for r in cleaned.risks:
        texts.append(("風險", r))
    for nd in cleaned.news_digest:
        texts.append(("新聞解讀", nd.takeaway))
    for where, text in texts:
        for w in _scan_phrases(text, ADVICE_BANNED):
            counts["phrase_warnings"] += 1
            add("advice", "warning", f"[{where}] 出現買賣建議禁語「{w}」")
        for w in _scan_phrases(text, CAUSALITY_BANNED):
            counts["phrase_warnings"] += 1
            add("causality", "warning", f"[{where}] 出現必然因果禁語「{w}」")

    # ── Data Age Guard ──
    if not cleaned.data_as_of:
        add("data_age", "error", "缺 data_as_of")

    errors = sum(1 for v in violations if v["severity"] == "error")
    report = {
        "passed": errors == 0,
        "checked_at": datetime.now(ZoneInfo(settings.tz)).isoformat(),
        "counts": counts,
        "error_count": errors,
        "warning_count": sum(1 for v in violations if v["severity"] == "warning"),
        "violations": violations,
    }
    return cleaned, report
