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

from .prompts import (
    build_brief_research_prompt,
    build_brief_structuring_prompt,
    build_full_brief_prompt,
    build_intermarket_prompt,
)
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
    """整理 usageMetadata 成計費所需欄位（對齊 cost.tracker.cost_of_usage）。

    - input_tokens＝promptTokenCount（Google 後台口徑，**已含** cached_tokens）
    - cached_tokens＝命中快取部分（以較低 cache 價計，避免高估）
    - output_tokens＝candidatesTokenCount + thoughtsTokenCount（計費含 thinking，否則嚴重低估）
    - tool_tokens＝toolUsePromptTokenCount（與 promptTokenCount 分開，按 input 價計）
    """
    um = body.get("usageMetadata", {}) or {}
    output = int(um.get("candidatesTokenCount", 0)) + int(um.get("thoughtsTokenCount", 0))
    return {
        "input_tokens": int(um.get("promptTokenCount", 0)),
        "cached_tokens": int(um.get("cachedContentTokenCount", 0)),
        "output_tokens": output,
        "tool_tokens": int(um.get("toolUsePromptTokenCount", 0)),
    }


def _grounding_sources(candidate: dict) -> list[tuple[str, str]]:
    """從 groundingMetadata 取 (標題, url) 清單。"""
    gm = candidate.get("groundingMetadata", {}) or {}
    out: list[tuple[str, str]] = []
    for ch in gm.get("groundingChunks", []) or []:
        w = ch.get("web", {}) or {}
        if w.get("uri"):
            out.append((w.get("title") or w["uri"], w["uri"]))
    return out


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


def analyze_full_brief_grounded(
    features: dict[str, Any],
) -> tuple[BriefResult, dict[str, int], dict[str, int]]:
    """兩段式即時連網晨報：①PRO+Google 搜尋 寫分析稿（主推理連網）→ ②Flash 純格式化成 BriefResult。

    回 (result, 研究階段 usage, 格式化階段 usage)。突破 responseSchema 不能與 tool 並用的限制。
    """
    analysis, research_usage = generate_text(
        build_brief_research_prompt(features),
        model=settings.gemini_model_brief,
        use_search=True,
    )
    raw, struct_usage = _generate_json(
        build_brief_structuring_prompt(analysis, features),
        GEMINI_BRIEF_SCHEMA,
        model=settings.gemini_model_qa,
    )
    return BriefResult.model_validate(raw), research_usage, struct_usage


@retry(
    retry=retry_if_exception_type((httpx.RequestError, GeminiUnavailable)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    reraise=True,
)
@retry(
    retry=retry_if_exception_type((httpx.RequestError, GeminiUnavailable)),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    reraise=True,
)
def generate_text(
    prompt: str, model: str | None = None, *, use_search: bool = False
) -> tuple[str, dict[str, int]]:
    """純文字生成（Discord Q&A / 市場情境）。use_search=True 啟用 Google 搜尋 grounding，
    並把來源附在文末。回 (文字, token usage)。503 會重試、429 fail-fast。"""
    if not settings.gemini_api_key.strip():
        raise GeminiError("GEMINI_API_KEY 未設定")
    url = f"{BASE_URL}/{model or settings.gemini_model_qa}:generateContent"
    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }
    if use_search:
        # Gemini 2.x 研究工具：搜尋公開網路 + 讀網頁內容 + 跑運算（API 需明確帶，非預設）
        payload["tools"] = [
            {"google_search": {}},
            {"url_context": {}},
            {"code_execution": {}},
        ]
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
    cand = candidates[0]
    parts = cand.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    if use_search:
        srcs = _grounding_sources(cand)
        if srcs:
            text += "\n\n🔎 來源：" + "　".join(f"[{t}]({u})" for t, u in srcs[:4])
    return text, _usage_of(body)


def fetch_web_context(date_str: str, model: str | None = None) -> tuple[str, dict[str, int]]:
    """用 Google 搜尋抓近兩日影響台/美/加密市場的重大事件（附來源），給晨報強化事實基礎。"""
    prompt = (
        f"用 Google 搜尋，列出 {date_str} 前後最新、會影響台股／美股／加密貨幣市場的重大事件"
        f"（總經數據、央行/利率、政策、地緣政治、重大企業或產業消息）。"
        f"繁體中文條列 5–8 點，每點一句話並標日期，只列最近兩天、務必基於可信來源。"
    )
    return generate_text(prompt, model=model or settings.gemini_model_qa, use_search=True)
