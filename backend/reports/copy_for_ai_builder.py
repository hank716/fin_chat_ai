"""Copy-for-AI builder（M2-report，對齊 design §6.2 第15項「給其他 AI 的分析包」）。

產出 Web 報告底部可一鍵複製、貼到 Claude/ChatGPT 的深度分析 prompt。
系統不自動呼叫 Claude（ARCHITECTURE §4.1），由使用者自己決定。
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from ai.schemas import BriefResult

USAGE_LIMITS = """請不要自行編造市場數據。
請不要引用沒有來源的新聞。
如果資料不足，請明確說明需要補哪些資料。
跨市場連動只能視為可能影響，不可視為必然因果。
候選觀察標的僅供研究，不是買賣建議；請勿給出目標價或進出場點。
此內容僅供家庭內部市場研究，不構成投資建議。"""

ANALYSIS_QUESTIONS = """1. 今天台股最需要注意哪些風險？
2. 哪些族群可能受到前一日美股或加密貨幣影響？哪些只是消息面、哪些有量價與籌碼支撐？
3. 候選觀察標的中，哪些只是短線題材，哪些可能有較完整的基本面或籌碼支撐？
4. 三大法人籌碼動向透露什麼？外資與投信是否同調？
5. 哪些地方資料不足？還應該補哪些指標或新聞來源？
6. 請保守處理推論，明確區分事實與你的判讀，不要編造資料。"""


def build_copy_for_ai(
    result: BriefResult,
    *,
    report_type: str = "每日跨市場晨報",
    raw_query: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    ts = (generated_at or datetime.now(ZoneInfo(settings.tz))).strftime("%Y-%m-%d %H:%M %Z")
    json_summary = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)

    return f"""# 給其他 AI 的分析包

請根據以下這份今日市場晨報，協助我做進一步的深度分析。

## 使用限制

{USAGE_LIMITS}

## 報告資訊

- 報告時間：{ts}
- 報告類型：{report_type}
- 使用者問題：{raw_query or "（每日例行晨報，無特定提問）"}
- 涵蓋市場：美股、加密貨幣、台股（含三大法人籌碼與新聞）
- 資料日期：{result.data_as_of.isoformat()}

## 今日簡短結論

{result.headline}

## 完整晨報（結構化 JSON：含各段敘事、evidence 來源欄位、候選標的、風險、新聞）

```json
{json_summary}
```

## 請協助分析

{ANALYSIS_QUESTIONS}
"""
