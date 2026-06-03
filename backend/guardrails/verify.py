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


# path 段：base key 後可接零或多個清單索引，如 top_foreign_buy_5d[0] 或 matrix[1][2]
_PATH_PART_RE = re.compile(r"^([^\[\]]+)((?:\[\d+\])*)$")


def _resolve(features: dict[str, Any], ref: str) -> Any:
    """解析 source_ref 到 features 內的值。

    支援 dict key 與清單索引混用，例如：
      features.tw.index.return_20d_pct
      features.tw.movers.top_foreign_buy_5d[0].foreign_net_buy_5d_lots
    模型引用 movers 排行（list of dict）時必帶 [i]，舊版只走 dict key 會把真實數值
    誤判為捏造而移除，故此處需解析索引段。
    """
    parts = ref.split(".")
    if parts and parts[0] == "features":
        parts = parts[1:]
    cur: Any = features
    for p in parts:
        m = _PATH_PART_RE.match(p)
        if not m:
            return _MISSING
        key, idx_str = m.group(1), m.group(2)
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return _MISSING
        for idx in re.findall(r"\[(\d+)\]", idx_str):
            i = int(idx)
            if isinstance(cur, (list, tuple)) and 0 <= i < len(cur):
                cur = cur[i]
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
