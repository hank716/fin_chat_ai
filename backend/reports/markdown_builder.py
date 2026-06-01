"""Markdown report builder（M1 step 4，對齊 design §19.2）。

從結構化 AnalysisResult render 人看的 md。M1 只有 intermarket section，
故 section 動態跑 result.sections，不硬寫死技術/基本/籌碼/新聞。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from ai.schemas import AnalysisResult, Claim

CLAIM_TAG = {
    "fact": "事實",
    "calculation": "計算",
    "inference": "推論",
    "limitation": "限制",
}


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


def _render_claims(claims: list[Claim]) -> str:
    if not claims:
        return "_（無）_"
    lines = []
    for c in claims:
        tag = CLAIM_TAG.get(c.claim_type, c.claim_type)
        ref = f"（來源：`{c.source_ref}`）" if c.source_ref else ""
        lines.append(f"- **[{tag}]** {c.text}{ref}")
    return "\n".join(lines)


def build_markdown(
    result: AnalysisResult,
    *,
    report_type: str = "每日跨市場晨報",
    raw_query: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    ts = (generated_at or _now()).strftime("%Y-%m-%d %H:%M %Z")
    parts: list[str] = []
    parts.append("# AI 多市場研究報告")
    parts.append(
        f"> {report_type}　•　產生時間：{ts}　•　資料日期：{result.data_as_of.isoformat()}"
    )
    if raw_query:
        parts.append(f"## 使用者問題\n\n{raw_query}")
    parts.append(f"## 簡短結論\n\n{result.summary}")
    parts.append(
        "## 使用資料\n\n"
        f"- 資料日期（Data As Of）：{result.data_as_of.isoformat()}\n"
        f"- 引用 input 欄位數：{len(result.sources)}"
    )

    # 市場觀察：limitation 抽出到最後的「風險與限制」
    risk_claims: list[Claim] = []
    for sec in result.sections:
        non_limit = [c for c in sec.claims if c.claim_type != "limitation"]
        risk_claims.extend(c for c in sec.claims if c.claim_type == "limitation")
        parts.append(f"## {sec.section}\n\n{_render_claims(non_limit)}")

    parts.append(f"## 風險與限制\n\n{_render_claims(risk_claims)}")
    parts.append(
        "---\n*本報告由系統自動產生，僅供家庭內部市場研究，不構成投資建議。*"
    )
    return "\n\n".join(parts)
