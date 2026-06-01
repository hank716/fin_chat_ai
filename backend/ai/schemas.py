"""結構化分析輸出 schema（M1 step 3，對齊 ARCHITECTURE §4.4）。

模型直接輸出結構化 JSON，每個 claim 標類型與來源；md/web/copy-for-ai 全從這份 render。
guardrail（M5）會驗證 source_ref 是否存在於 input。
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

ClaimType = Literal["fact", "calculation", "inference", "limitation"]


class Claim(BaseModel):
    text: str
    claim_type: ClaimType
    # 指向 input JSON 欄位（如 "assets.SOX.return_20d_pct"）或新聞 url；
    # inference / limitation 可為 None，但需在 text 註明依據。
    source_ref: str | None = None


class ReportSection(BaseModel):
    section: str  # technical / intermarket / risk / ...
    claims: list[Claim] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    summary: str
    sections: list[ReportSection] = Field(default_factory=list)
    data_as_of: date
    sources: list[str] = Field(default_factory=list)


# Gemini responseSchema（OpenAPI 子集；強制模型回符合 AnalysisResult 的 JSON）
GEMINI_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "claim_type": {
                                    "type": "string",
                                    "enum": ["fact", "calculation", "inference", "limitation"],
                                },
                                "source_ref": {"type": "string", "nullable": True},
                            },
                            "required": ["text", "claim_type"],
                        },
                    },
                },
                "required": ["section", "claims"],
            },
        },
        "data_as_of": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "sections", "data_as_of", "sources"],
}
