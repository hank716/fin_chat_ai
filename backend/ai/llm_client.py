"""LLM 抽象層（ARCHITECTURE §4.1）。

系統內只用 Gemini；保留一層薄 Protocol 方便日後換供應商，**不接 Claude API**。
"""
from __future__ import annotations

from typing import Any, Protocol

from . import gemini_client
from .schemas import AnalysisResult


class LLMClient(Protocol):
    def analyze_intermarket(self, features: dict[str, Any]) -> AnalysisResult: ...


class GeminiClient:
    def analyze_intermarket(self, features: dict[str, Any]) -> AnalysisResult:
        return gemini_client.analyze_intermarket(features)


def get_llm_client() -> LLMClient:
    return GeminiClient()
