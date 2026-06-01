"""Copy-for-AI builder（M1 step 4，對齊 design §8.3）。

產出 Web 報告底部「給其他 AI 的分析包」——一段可一鍵複製貼到 Claude/ChatGPT 的
深度分析 prompt。系統不自動呼叫 Claude（ARCHITECTURE §4.1），由使用者自己決定。
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from ai.schemas import AnalysisResult, Claim

USAGE_LIMITS = """請不要自行編造市場數據。
請不要引用沒有來源的新聞。
如果資料不足，請明確說明需要補哪些資料。
跨市場連動只能視為可能影響，不可視為必然因果。
此內容僅供家庭內部市場研究，不構成投資建議。"""

ANALYSIS_QUESTIONS = """1. 今天台股最需要注意哪些風險？
2. 哪些族群可能受到前一日美股或加密貨幣影響？
3. 候選觀察標的中，哪些只是短線題材，哪些可能有較完整支撐？
4. 哪些地方資料不足？
5. 還應該補哪些指標或新聞來源？
6. 請不要自行編造資料。
7. 請將事實、計算、推論與限制分開說明。"""

CLAIM_TAG = {"fact": "事實", "calculation": "計算", "inference": "推論", "limitation": "限制"}


def _render_claims(claims: list[Claim]) -> str:
    lines = []
    for c in claims:
        tag = CLAIM_TAG.get(c.claim_type, c.claim_type)
        ref = f"（來源：{c.source_ref}）" if c.source_ref else ""
        lines.append(f"- [{tag}] {c.text}{ref}")
    return "\n".join(lines) if lines else "（無）"


def build_copy_for_ai(
    result: AnalysisResult,
    *,
    report_type: str = "每日跨市場晨報",
    raw_query: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    ts = (generated_at or datetime.now(ZoneInfo(settings.tz))).strftime("%Y-%m-%d %H:%M %Z")

    section_blocks = []
    for sec in result.sections:
        section_blocks.append(f"### {sec.section}\n{_render_claims(sec.claims)}")
    sections_text = "\n\n".join(section_blocks)

    json_summary = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)

    return f"""# 給其他 AI 的分析包

請根據以下資料協助進一步分析市場狀況。

## 使用限制

{USAGE_LIMITS}

## 報告資訊

- 報告時間：{ts}
- 報告類型：{report_type}
- 使用者問題：{raw_query or "（每日例行晨報，無特定提問）"}
- 主要市場：美股、加密貨幣（M1 範圍；台股/籌碼/新聞後續里程碑加入）
- 資料日期：{result.data_as_of.isoformat()}

## 市場摘要

{result.summary}

## 各面向觀察（已標註 事實／計算／推論／限制 與來源欄位）

{sections_text}

## 原始結構化 JSON

```json
{json_summary}
```

## 請協助分析

{ANALYSIS_QUESTIONS}
"""
