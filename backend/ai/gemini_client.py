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

from activity import monitor
from config import settings

from .errors import LLMBadRequest, LLMError, LLMQuotaExceeded, LLMUnavailable
from .prompts import (
    build_brief_research_prompt,
    build_brief_structuring_prompt,
    build_full_brief_prompt,
    build_intent_prompt,
    build_intermarket_prompt,
)
from .schemas import (
    GEMINI_BRIEF_SCHEMA,
    GEMINI_INTENT_SCHEMA,
    GEMINI_RESPONSE_SCHEMA,
    AnalysisResult,
    BriefResult,
)

logger = logging.getLogger("ai-market-backend.gemini")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
# 完整晨報 input/output 較大，模型回應可能 >60s；放寬 read timeout。
TIMEOUT = httpx.Timeout(180.0, connect=10.0)

# Gemini 研究工具：搜尋公開網路 + 讀網頁內容 + 跑運算（API 需明確帶，非預設）。
# ⚠️ 與明確快取（cachedContent）併用時，tools 必須**放進 CachedContent**、不可再放進
# generateContent 請求（否則 400），故下方 generate_text 在帶 cached_content 時不重送 tools。
SEARCH_TOOLS = [
    {"google_search": {}},
    {"url_context": {}},
    {"code_execution": {}},
]


# 供應商中立例外（spec 022）：Gemini 例外改為繼承之，既有 `except GeminiError` 呼叫點行為不變，
# 但上層也能用 `except LLMError` 同時涵蓋 Gemini 與 Claude 兩條路徑。
class GeminiError(LLMError):
    pass


class GeminiUnavailable(GeminiError, LLMUnavailable):
    """暫時性過載（503）→ 值得 retry。"""


class GeminiQuotaExceeded(GeminiError, LLMQuotaExceeded):
    """配額用盡（429）→ 短時間內不會恢復，fail-fast 不 retry。"""


class GeminiBadRequest(GeminiError, LLMBadRequest):
    """請求被拒（400）→ 不 retry。明確快取與 tools 不相容等情況會回 400，呼叫端據此降級。"""


# 對外 HTTP 呼叫的共用重試策略：只重試暫時性過載(503→GeminiUnavailable)與連線類錯誤
# (httpx.RequestError)；429/400 由各函式 fail-fast、不進 retry。套在**真正發 HTTP 的**
# _generate_json / generate_text（純資料整理的 _usage_of 不該套）。stop=4＝最多 4 次嘗試。
_gemini_retry = retry(
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


@_gemini_retry
def _generate_json(
    prompt: str, response_schema: dict, model: str | None = None,
    *, cached_content: str | None = None, temperature: float = 0.4,
) -> tuple[dict[str, Any], dict[str, int]]:
    if not settings.gemini_api_key.strip():
        raise GeminiError("GEMINI_API_KEY 未設定")
    url = f"{BASE_URL}/{model or settings.gemini_model}:generateContent"
    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "temperature": temperature,
        },
    }
    if cached_content:
        # 靜態前綴已存進明確快取；contents 只放變動部分，頂層帶 cachedContent 名稱。
        payload["cachedContent"] = cached_content
    headers = {"Content-Type": "application/json", "X-goog-api-key": settings.gemini_api_key}
    resp = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    monitor.mark("ai")  # 記一次對外 AI 流量（待機偵測用）
    if resp.status_code == 503:
        raise GeminiUnavailable(f"Gemini 503 overloaded: {resp.text[:160]}")
    if resp.status_code == 429:
        # 每日/每分鐘配額；retry 4 次只會白等，直接 fail-fast 讓上層回清楚訊息
        raise GeminiQuotaExceeded(f"Gemini 429 quota exceeded: {resp.text[:200]}")
    if resp.status_code == 400:
        # 通常是請求本身有問題（如 cachedContent 與 tools/schema 不相容）；不 retry，讓上層降級。
        raise GeminiBadRequest(f"Gemini 400 bad request: {resp.text[:300]}")
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


def classify_finance_intent(question: str) -> tuple[bool, dict[str, int]]:
    """用最便宜的模型判斷問題是否與財務市場相關，回 (is_financial, token usage)。

    極小 prompt（只含問題）＋極小 schema、不開搜尋；用來在問答前過濾掉閒聊，省下大 prompt 與
    grounding。**fail-open**：任何錯誤（HTTP 失敗、解析失敗、配額）一律回 True，讓問答照常進行，
    分類器絕不能成為問答的單點故障。
    """
    try:
        raw, usage = _generate_json(
            build_intent_prompt(question),
            GEMINI_INTENT_SCHEMA,
            model=settings.gemini_model_classifier,
        )
        return bool(raw.get("is_financial", True)), usage
    except GeminiError as exc:
        logger.warning("意圖分類失敗，fail-open 視為財務相關: %s", exc)
        return True, {}


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
    features: dict[str, Any], calibration: str | None = None,
) -> tuple[BriefResult, dict[str, int], dict[str, int]]:
    """兩段式即時連網晨報：①PRO+Google 搜尋 寫分析稿（主推理連網）→ ②Flash 純格式化成 BriefResult。

    回 (result, 研究階段 usage, 格式化階段 usage)。突破 responseSchema 不能與 tool 並用的限制。
    calibration：選用的回測校準提示，注入研究階段 prompt 讓模型依過去準確度自我修正選股傾向。
    """
    analysis, research_usage = generate_text(
        build_brief_research_prompt(features, calibration=calibration),
        model=settings.gemini_model_brief,
        use_search=True,
    )
    raw, struct_usage = _generate_json(
        build_brief_structuring_prompt(analysis, features),
        GEMINI_BRIEF_SCHEMA,
        model=settings.gemini_model_qa,
        temperature=0.1,     # 第②段純格式化：趨近確定性，降低『新增事實/數字』的幻覺風險
    )
    return BriefResult.model_validate(raw), research_usage, struct_usage


@_gemini_retry
def generate_text_with_candidate(
    prompt: str, model: str | None = None, *, use_search: bool = False,
    cached_content: str | None = None,
) -> tuple[str, dict[str, int], dict]:
    """同 `generate_text`，但**額外回傳 candidate 原始 dict**。

    召回層（`retrieval.py`）需要完整的 `groundingMetadata.groundingChunks` 才能把每則線索
    對應回來源 URL；`generate_text` 只把前 4 筆來源貼在文末（那是給 Discord 看的），
    對查證來說不夠——漏掉的 URL 等於決策層沒辦法查證那條事實。
    """
    if not settings.gemini_api_key.strip():
        raise GeminiError("GEMINI_API_KEY 未設定")
    url = f"{BASE_URL}/{model or settings.gemini_model_qa}:generateContent"
    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }
    if cached_content:
        payload["cachedContent"] = cached_content
    # 帶明確快取時 tools 已在快取內，不可再放進請求（會 400）；否則照常帶。
    if use_search and not cached_content:
        payload["tools"] = SEARCH_TOOLS
    headers = {"Content-Type": "application/json", "X-goog-api-key": settings.gemini_api_key}
    resp = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    monitor.mark("ai")  # 記一次對外 AI 流量（待機偵測用）
    if resp.status_code == 503:
        raise GeminiUnavailable(f"Gemini 503 overloaded: {resp.text[:160]}")
    if resp.status_code == 429:
        raise GeminiQuotaExceeded(f"Gemini 429 quota exceeded: {resp.text[:200]}")
    if resp.status_code == 400:
        raise GeminiBadRequest(f"Gemini 400 bad request: {resp.text[:300]}")
    if resp.status_code != 200:
        raise GeminiError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise GeminiError(f"Gemini 無 candidates: {json.dumps(body)[:300]}")
    cand = candidates[0]
    parts = cand.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    return text, _usage_of(body), cand


def generate_text(
    prompt: str, model: str | None = None, *, use_search: bool = False,
    cached_content: str | None = None,
) -> tuple[str, dict[str, int]]:
    """純文字生成（Discord Q&A / 市場情境）。use_search=True 啟用 Google 搜尋 grounding，
    並把來源附在文末。cached_content 非 None 時帶明確快取（contents 只放變動部分）。
    回 (文字, token usage)。503 會重試、429/400 fail-fast。"""
    text, usage, cand = generate_text_with_candidate(
        prompt, model=model, use_search=use_search, cached_content=cached_content,
    )
    if use_search:
        srcs = _grounding_sources(cand)
        if srcs:
            text += "\n\n🔎 來源：" + "　".join(f"[{t}]({u})" for t, u in srcs[:4])
    return text, usage
