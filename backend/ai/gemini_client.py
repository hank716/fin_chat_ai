"""Gemini client（M1 step 3）。

用 generativelanguage v1beta generateContent + X-goog-api-key（對齊使用者驗證過的 curl），
強制 responseMimeType=application/json + responseSchema，輸出 parse 成 AnalysisResult。

系統內唯一 LLM（ARCHITECTURE §4.1）。503 high-demand / 429 會 tenacity 重試。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import settings

from .prompts import build_full_brief_prompt, build_intermarket_prompt
from .schemas import GEMINI_BRIEF_SCHEMA, GEMINI_RESPONSE_SCHEMA, AnalysisResult, BriefResult

logger = logging.getLogger("ai-market-backend.gemini")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
# 完整晨報 input/output 較大，模型回應可能 >60s；放寬 read timeout。
TIMEOUT = httpx.Timeout(180.0, connect=10.0)


class GeminiError(RuntimeError):
    pass


class GeminiUnavailable(GeminiError):
    """暫時性過載（503）→ 值得 retry。"""


class GeminiQuotaExceeded(GeminiError):
    """配額用盡（429）→ 短時間內不會恢復，fail-fast 不 retry。"""


@retry(
    retry=retry_if_exception_type((httpx.RequestError, GeminiUnavailable)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    reraise=True,
)
def _usage_of(body: dict) -> dict[str, int]:
    um = body.get("usageMetadata", {}) or {}
    return {
        "input_tokens": int(um.get("promptTokenCount", 0)),
        "output_tokens": int(um.get("candidatesTokenCount", 0)),
    }


def _generate_json(
    prompt: str, response_schema: dict, model: str | None = None
) -> tuple[dict[str, Any], dict[str, int]]:
    if not settings.gemini_api_key.strip():
        raise GeminiError("GEMINI_API_KEY 未設定")
    url = f"{BASE_URL}/{model or settings.gemini_model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "temperature": 0.4,
        },
    }
    headers = {"Content-Type": "application/json", "X-goog-api-key": settings.gemini_api_key}
    resp = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 503:
        raise GeminiUnavailable(f"Gemini 503 overloaded: {resp.text[:160]}")
    if resp.status_code == 429:
        # 每日/每分鐘配額；retry 4 次只會白等，直接 fail-fast 讓上層回清楚訊息
        raise GeminiQuotaExceeded(f"Gemini 429 quota exceeded: {resp.text[:200]}")
    if resp.status_code != 200:
        raise GeminiError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise GeminiError(f"Gemini 無 candidates: {json.dumps(body)[:300]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise GeminiError("Gemini 回空內容")
    try:
        return json.loads(text), _usage_of(body)
    except json.JSONDecodeError as exc:
        raise GeminiError(f"Gemini 回的不是合法 JSON: {exc}; head={text[:200]}") from exc


def analyze_intermarket(features: dict[str, Any]) -> AnalysisResult:
    """features JSON → 結構化 AnalysisResult（M1 intermarket，保留供相容）。"""
    prompt = build_intermarket_prompt(features)
    raw, _usage = _generate_json(prompt, GEMINI_RESPONSE_SCHEMA)
    return AnalysisResult.model_validate(raw)


def analyze_full_brief(features: dict[str, Any]) -> tuple[BriefResult, dict[str, int]]:
    """合併 features → 完整敘事晨報 BriefResult + Gemini token usage（晨報用 PRO 模型）。"""
    prompt = build_full_brief_prompt(features)
    raw, usage = _generate_json(prompt, GEMINI_BRIEF_SCHEMA, model=settings.gemini_model_brief)
    return BriefResult.model_validate(raw), usage


@retry(
    retry=retry_if_exception_type((httpx.RequestError, GeminiUnavailable)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    reraise=True,
)
def generate_text(prompt: str, model: str | None = None) -> tuple[str, dict[str, int]]:
    """純文字生成（Discord 互動 Q&A 用，預設 Flash），回 (文字, token usage)。"""
    if not settings.gemini_api_key.strip():
        raise GeminiError("GEMINI_API_KEY 未設定")
    url = f"{BASE_URL}/{model or settings.gemini_model_qa}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }
    headers = {"Content-Type": "application/json", "X-goog-api-key": settings.gemini_api_key}
    resp = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 503:
        raise GeminiUnavailable(f"Gemini 503 overloaded: {resp.text[:160]}")
    if resp.status_code == 429:
        raise GeminiQuotaExceeded(f"Gemini 429 quota exceeded: {resp.text[:200]}")
    if resp.status_code != 200:
        raise GeminiError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise GeminiError(f"Gemini 無 candidates: {json.dumps(body)[:300]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    um = body.get("usageMetadata", {}) or {}
    usage = {
        "input_tokens": int(um.get("promptTokenCount", 0)),
        "output_tokens": int(um.get("candidatesTokenCount", 0)),
    }
    return text, usage
