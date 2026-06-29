"""Markdown report builder（M2-report：敘事晨報，對齊 design §6.2）。

從 BriefResult render 人看的 md：簡短結論 → 各敘事 section（段落 + 可稽核數字）
→ 候選觀察標的 → 風險提醒 → 後續追蹤 → 重要新聞 → 資料來源。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from ai.schemas import BriefResult, BriefSection


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def _render_section(sec: BriefSection) -> str:
    parts = [f"## {sec.title}", "", sec.narrative]
    if sec.evidence:
        parts.append("")
        for e in sec.evidence:
            ref = f" `（{e.source_ref}）`" if e.source_ref else ""
            parts.append(f"- {e.label}：**{e.value}**{ref}")
    return "\n".join(parts)


def build_markdown(
    result: BriefResult,
    *,
    report_type: str = "每日跨市場晨報",
    raw_query: str | None = None,
    generated_at: datetime | None = None,
    cost: dict | None = None,
) -> str:
    ts = (generated_at or _now()).strftime("%Y-%m-%d %H:%M %Z")
    parts: list[str] = []
    parts.append("# AI 多市場研究報告")
    parts.append(
        f"> {report_type}　•　產生時間：{ts}　•　資料日期：{result.data_as_of.isoformat()}"
    )
    if raw_query:
        parts.append(f"## 使用者問題\n\n{raw_query}")

    parts.append(f"## 今日簡短結論\n\n{result.headline}")

    for sec in result.sections:
        parts.append(_render_section(sec))

    def _watch_block(title: str, note: str, items: list) -> str:
        lines = [f"## {title}", "", f"_{note}_", ""]
        for w in items:
            sector = f"〔{w.sector}〕" if w.sector else ""
            lines.append(f"### {w.symbol} {w.name} {sector}")
            lines.append(w.thesis)
            if w.signals:
                lines.append("")
                lines.append("訊號：" + "、".join(w.signals))
            if w.target_price or w.stop_loss:
                bits = []
                if w.target_price:
                    bits.append(f"🎯 目標價 **{w.target_price}**")
                if w.stop_loss:
                    bits.append(f"🛑 止損價 **{w.stop_loss}**")
                lines.append("\n" + "　".join(bits) + "（技術面參考，非保證）")
            if w.uncertainty:
                lines.append(f"\n> ⚠️ 待驗證：{w.uncertainty}")
            lines.append("")
        return "\n".join(lines).rstrip()

    if result.tw_watchlist:
        parts.append(_watch_block("偏多觀察標的（含目標價/止損）", "技術面偏多；目標價/止損為技術參考，非保證，請自行判斷。", result.tw_watchlist))
    if result.tw_caution:
        parts.append(_watch_block("偏空/要注意標的", "技術面偏空或有追高/籌碼風險；目標價/止損為技術參考，非保證。", result.tw_caution))

    if result.risks:
        parts.append("## 今日風險提醒\n\n" + "\n".join(f"- {r}" for r in result.risks))

    if result.follow_ups:
        parts.append("## 後續追蹤重點\n\n" + "\n".join(f"- {f}" for f in result.follow_ups))

    if result.news_digest:
        lines = ["## 重要新聞與事件", ""]
        _PROVIDER_LABEL = {"finmind": "FinMind", "google": "Google Finance"}
        for n in result.news_digest:
            title = f"[{n.title}]({n.url})" if n.url else n.title
            meta = f"{n.source}・{n.date}"
            via = _PROVIDER_LABEL.get(n.provider or "")
            if via:
                meta += f"・via {via}"
            lines.append(f"- **{title}**　_{meta}_")
            lines.append(f"  - 解讀：{n.takeaway}")
            if n.uncertainty:
                lines.append(f"  - 不確定性：{n.uncertainty}")
        parts.append("\n".join(lines))

    source_lines = [
        f"- 資料日期（Data As Of）：{result.data_as_of.isoformat()}",
        f"- 引用 features 欄位／來源數：{len(result.sources)}",
    ]
    if cost:
        source_lines.append(
            f"- AI 花費（全站）：本篇 NT${cost.get('brief_twd', 0):.4f}"
            f"・今日 NT${cost.get('day_total_twd', 0):.2f}"
            f"・{cost.get('month', '')} 本月 NT${cost.get('month_total_twd', 0):.2f}"
            f" / NT${cost.get('monthly_limit_twd', 0):.0f}"
        )
    parts.append("## 資料來源\n\n" + "\n".join(source_lines))
    parts.append(
        "---\n*⚠️ 本報告之方向看法、目標價與止損價皆為技術面輔助參考，非保證獲利或準確，"
        "亦不保證成交；請自行評估風險後決策。系統自動產生，僅供家庭內部研究。*"
    )
    return "\n\n".join(parts)
