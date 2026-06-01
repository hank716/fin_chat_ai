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

from .prompts import build_intermarket_prompt
from .schemas import GEMINI_RESPONSE_SCHEMA, AnalysisResult

logger = logging.getLogger("ai-market-backend.gemini")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class GeminiError(RuntimeError):
    pass


class GeminiUnavailable(GeminiError):
    """暫時性錯誤（503/429）→ 值得 retry。"""


@retry(
    retry=retry_if_exception_type((httpx.RequestError, GeminiUnavailable)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    reraise=True,
)
def _generate_json(prompt: str, response_schema: dict) -> dict[str, Any]:
    if not settings.gemini_api_key.strip():
        raise GeminiError("GEMINI_API_KEY 未設定")
    url = f"{BASE_URL}/{settings.gemini_model}:generateContent"
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
    if resp.status_code in (429, 503):
        raise GeminiUnavailable(f"Gemini {resp.status_code}: {resp.text[:160]}")
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
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiError(f"Gemini 回的不是合法 JSON: {exc}; head={text[:200]}") from exc


def analyze_intermarket(features: dict[str, Any]) -> AnalysisResult:
    """features JSON → 結構化 AnalysisResult（唯一系統內 LLM 呼叫點）。"""
    prompt = build_intermarket_prompt(features)
    raw = _generate_json(prompt, GEMINI_RESPONSE_SCHEMA)
    return AnalysisResult.model_validate(raw)
