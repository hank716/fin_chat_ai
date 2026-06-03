"""Gemini 明確快取（cachedContents API）。

同一天大量 /ask 會重複送幾乎相同的「當日靜態 context」（QA 規則 + 晨報 markdown + 完整 features
JSON），動輒數萬～數十萬 token。把這塊一次性存進 Gemini 明確快取（綁 report_id + model），之後每題
只送變動部分並引用該快取名稱，命中部分以較低 cache 價計（cost.tracker 已支援），大幅省 input token。

設計：
- 快取名稱存 Redis `gemini:cache:{model}:{report_id}`，TTL 對齊 cachedContents 的 ttl（短於它一點，
  避免引用到剛過期的）。
- **優雅降級**：建立失敗（context 太短未達門檻、API 拒絕、Redis 故障…）一律回 None，呼叫端改走
  完整 prompt（仍可吃 Gemini 隱式快取），問答不中斷。
- 明確快取綁定 model 名稱，必須與後續 generateContent 用的 model 一致。
"""
from __future__ import annotations

import logging

import httpx

from config import settings
from redis_client import redis_client

from .gemini_client import SEARCH_TOOLS

logger = logging.getLogger("ai-market-backend.gemini-cache")

CACHE_URL = "https://generativelanguage.googleapis.com/v1beta/cachedContents"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


# key 版本：快取結構改變（如把 tools 併入快取）時 bump，避免沿用舊結構的快取名稱。
_KEY_VERSION = "v2"


def _redis_key(model: str, report_id: str) -> str:
    return f"gemini:cache:{_KEY_VERSION}:{model}:{report_id}"


def get_or_create_qa_cache(report_id: str, static_block: str, model: str) -> str | None:
    """取得（或建立）當日 QA 靜態 context 的明確快取，回 cachedContents 名稱；任何失敗回 None。

    QA 路徑會用 Google 搜尋，故把 tools **一併放進快取**（cachedContent 帶 tools），之後
    generateContent 引用此快取時就不再重送 tools（否則 Gemini 會回 400）。
    """
    if not settings.enable_gemini_explicit_cache or not report_id:
        return None
    if not settings.gemini_api_key.strip():
        return None

    rkey = _redis_key(model, report_id)
    try:
        cached = redis_client.get(rkey)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else str(cached)
    except Exception as exc:  # noqa: BLE001
        logger.debug("讀取快取名稱失敗（改建新的）: %s", exc)

    ttl = int(settings.gemini_cache_ttl_seconds)
    payload = {
        # cachedContents 的 model 需 "models/" 前綴，且要與 generateContent 用的 model 一致
        "model": f"models/{model}",
        "contents": [{"role": "user", "parts": [{"text": static_block}]}],
        # tools 放進快取（QA 走搜尋）；引用此快取的請求不可再帶 tools。
        "tools": SEARCH_TOOLS,
        "ttl": f"{ttl}s",
    }
    headers = {"Content-Type": "application/json", "X-goog-api-key": settings.gemini_api_key}
    try:
        resp = httpx.post(CACHE_URL, json=payload, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("建立 Gemini 明確快取連線失敗（降級為隱式快取）: %s", exc)
        return None
    if resp.status_code != 200:
        # 最常見：context 未達該模型快取最低 token 門檻 → 400。降級即可，不必擾民。
        logger.info("建立 Gemini 明確快取未成功 HTTP %s（降級為隱式快取）: %s",
                    resp.status_code, resp.text[:200])
        return None

    name = (resp.json() or {}).get("name")
    if not name:
        return None
    try:
        # Redis TTL 比 cachedContents ttl 早 2 分鐘過期，避免引用到剛過期的快取名稱。
        redis_client.set(rkey, name, ex=max(ttl - 120, 60))
    except Exception as exc:  # noqa: BLE001
        logger.debug("寫入快取名稱失敗（不影響本次使用）: %s", exc)
    logger.info("已建立 Gemini 明確快取 %s（report=%s model=%s ttl=%ss）",
                name, report_id, model, ttl)
    return name
