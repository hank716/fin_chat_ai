"""Verification Guardrail（M5，對齊 design_docs §18）。

檢查 Gemini 產出的 BriefResult 是否超出 features 資料範圍、是否引用不存在的數據/標的/新聞、
是否使用禁語（買賣建議 / 必然因果）。對「捏造類」違規直接移除該片段並記錄；對「禁語類」
標記警示。回傳清理後的 result + guardrail 報告（存進 report JSON、頁面顯示攔截狀態）。

六道 guard：Source(Metric) / Symbol / News Citation / Advice / Intermarket Causality / Data Age。
另做新聞分層強制：social 來源比對後標 tier，頁面以情緒訊號呈現。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from ai.schemas import BriefResult

_MISSING = object()

# design §18.2 Advice Guard 禁語（依設計明列 + 明確的買賣指令變體）。
# 註：不收「目標價」——新聞解讀常如實轉述分析師目標價，屬報導而非 AI 自行建議，易誤判。
ADVICE_BANNED = (
    "保證獲利", "一定會漲", "一定會跌", "現在應該買進", "現在應該賣出",
    "建議買進", "建議賣出", "滿倉", "無風險", "穩賺", "包賺", "必賺",
)
CAUSALITY_BANNED = (
    "一定造成", "必然導致", "保證影響", "隔日一定", "必漲", "必跌",
    "一定上漲", "一定下跌", "必然上漲", "必然下跌",
)


def _resolve(features: dict[str, Any], ref: str) -> Any:
    """解析 source_ref（如 features.tw.index.return_20d_pct）到 features 內的值。"""
    parts = ref.split(".")
    if parts and parts[0] == "features":
        parts = parts[1:]
    cur: Any = features
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
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
    tw_symbols = set((features.get("tw", {}) or {}).get("stocks", {}).keys())
    for attr, label in (("tw_watchlist", "正向候選"), ("tw_caution", "負向候選")):
        kept = []
        for w in getattr(cleaned, attr):
            if tw_symbols and w.symbol not in tw_symbols:
                counts["symbols_dropped"] += 1
                add("symbol", "error", f"{label} {w.symbol} {w.name} 不在資料範圍，已移除")
                continue
            kept.append(w)
        setattr(cleaned, attr, kept)

    # ── News Citation Guard：須有 source/date/title/url 且比對得到 features.news ──
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
        if not (nd.source and nd.date and nd.title and nd.url):
            counts["news_dropped"] += 1
            add("news", "error", f"新聞「{nd.title[:30]}」缺 source/date/url，已移除")
            continue
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
