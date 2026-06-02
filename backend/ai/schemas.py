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


# ─────────────────────────────────────────────────────────────────────────
# V2：完整晨報 schema（M2-report）。敘事段落 + 可稽核 evidence + 候選/風險/新聞，
# 取代 M1 的 claim 標籤清單，對齊 design_docs §6.2 的 15 段晨報。
# ─────────────────────────────────────────────────────────────────────────


class Evidence(BaseModel):
    """段落敘事背後的可稽核數字（保留來源欄位，維持『不得捏造』可驗證性）。"""

    label: str
    value: str
    source_ref: str | None = None  # features JSON 欄位路徑


class BriefSection(BaseModel):
    title: str
    narrative: str  # 敘事段落（繁中 markdown prose，非條列標籤）
    evidence: list[Evidence] = Field(default_factory=list)


class WatchItem(BaseModel):
    """候選觀察標的（觀察用途，非買賣建議）。"""

    symbol: str
    name: str
    sector: str | None = None
    thesis: str  # 為何值得觀察（敘事）
    signals: list[str] = Field(default_factory=list)  # 訊號標籤，如「投信連買4日」
    uncertainty: str | None = None  # 不確定性 / 需隔日驗證之處


class NewsDigestItem(BaseModel):
    title: str
    source: str
    date: str
    url: str | None = None
    takeaway: str  # AI 解讀（inference）
    uncertainty: str | None = None


class BriefResult(BaseModel):
    headline: str  # 今日簡短結論（敘事 3–5 句）
    sections: list[BriefSection] = Field(default_factory=list)
    tw_watchlist: list[WatchItem] = Field(default_factory=list)  # 正向：值得關注
    tw_caution: list[WatchItem] = Field(default_factory=list)  # 負向：需注意風險
    risks: list[str] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    news_digest: list[NewsDigestItem] = Field(default_factory=list)
    data_as_of: date
    sources: list[str] = Field(default_factory=list)


_EVIDENCE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "value": {"type": "string"},
            "source_ref": {"type": "string", "nullable": True},
        },
        "required": ["label", "value"],
    },
}

_WATCHITEM_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "name": {"type": "string"},
            "sector": {"type": "string", "nullable": True},
            "thesis": {"type": "string"},
            "signals": {"type": "array", "items": {"type": "string"}},
            "uncertainty": {"type": "string", "nullable": True},
        },
        "required": ["symbol", "name", "thesis"],
    },
}

GEMINI_BRIEF_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "narrative": {"type": "string"},
                    "evidence": _EVIDENCE_SCHEMA,
                },
                "required": ["title", "narrative"],
            },
        },
        "tw_watchlist": _WATCHITEM_SCHEMA,
        "tw_caution": _WATCHITEM_SCHEMA,
        "risks": {"type": "array", "items": {"type": "string"}},
        "follow_ups": {"type": "array", "items": {"type": "string"}},
        "news_digest": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "source": {"type": "string"},
                    "date": {"type": "string"},
                    "url": {"type": "string", "nullable": True},
                    "takeaway": {"type": "string"},
                    "uncertainty": {"type": "string", "nullable": True},
                },
                "required": ["title", "source", "date", "takeaway"],
            },
        },
        "data_as_of": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "sections", "data_as_of"],
}


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
