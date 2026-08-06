"""廣度召回層 — Gemini + google_search 產「待查證線索」（spec 022-llm-tiering）。

憲章 2.0.0 Principle I：此層**只負責找得到**，不做分析、方向判斷或選股。輸出是帶 source URL
的事實線索（facts pack），交給決策層逐條查證後才採信。

為什麼要這樣切：Gemini 的 grounding 會產生幻覺——錯置日期、把分析評論當成新聞、引用內容
農場。由同一個模型同時召回與決策時，沒有任何一方能稽核另一方，幻覺會被洗成「有來源」的
假事實直接寫進晨報。分層後，決策層用 `web_fetch` 開啟這裡給的 URL 核對，兩者互為對照。

**無來源的線索一律丟棄**——查證不了的東西不該進決策層。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from config import settings

from . import gemini_client
from .errors import LLMError

logger = logging.getLogger("ai-market-backend.retrieval")

_RETRIEVAL_RULES = """你是市場資訊的**檢索員**，不是分析師。

任務：用 Google 搜尋找出指定期間內、會影響台股／美股／加密貨幣的重大事件，逐條列出。

嚴格規則（違反視為失敗）：
1. 只做**事實陳述**：發生了什麼、什麼時候、誰報導的。**不得**做分析、解讀、方向判斷、
   漲跌預測或選股，也**不得**推薦、點名任何值得買賣的標的——那是決策層的工作，不是你的。
2. 每則 claim 必須**可回溯到一個具體的網頁來源**，並在 source 寫出媒體名稱。
   憑印象、無法指出來源的事情一律不要寫。
3. date 用事件發生（或報導）的日期，格式 YYYY-MM-DD。不確定就不要寫這則。
4. claim 一句話說完，帶上關鍵數字（如「央行升息 1 碼至 2.125%」）。
5. 繁體中文。5–10 則，寧缺勿濫。

輸出格式：只輸出 JSON，形如
{"events": [{"claim": "...", "date": "YYYY-MM-DD", "source": "媒體名", "url": "https://..."}]}
不要有 JSON 以外的任何文字。"""


class FactEvent(BaseModel):
    """一則待查證的線索。`verified` 由決策層回填，召回層一律留 None。"""

    claim: str
    date: str
    source: str
    url: str
    verified: str | None = None


class FactsPack(BaseModel):
    events: list[FactEvent] = []

    def to_prompt_json(self) -> str:
        return json.dumps(
            [e.model_dump(exclude={"verified"}) for e in self.events],
            ensure_ascii=False,
        )


def _all_grounding_urls(candidate: dict) -> list[str]:
    """取出**完整** grounding URL 清單。

    ⚠️ 不能用 `gemini_client._grounding_sources()` —— 那是給 Discord 文末附連結用的，
    呼叫端會截斷成前 4 筆。查證需要完整清單：漏掉的 URL 等於決策層沒辦法查證那條事實。
    """
    gm = candidate.get("groundingMetadata", {}) or {}
    urls: list[str] = []
    for chunk in gm.get("groundingChunks", []) or []:
        web = chunk.get("web", {}) or {}
        uri = web.get("uri")
        if uri and uri not in urls:
            urls.append(uri)
    return urls


def _attach_urls(events: list[dict[str, Any]], fallback_urls: list[str]) -> list[FactEvent]:
    """把 event 補上 URL 並丟掉補不到的。

    模型自己填的 url 優先；沒填就從 grounding 清單按序補（Gemini 常常在 groundingMetadata
    有來源、但沒把 url 寫進 JSON 欄位）。兩邊都沒有 → **丟棄**，因為查證不了。
    """
    out: list[FactEvent] = []
    spare = list(fallback_urls)
    for item in events:
        url = (item.get("url") or "").strip()
        if not url and spare:
            url = spare.pop(0)
        if not url:
            logger.info("召回層線索無來源，丟棄：%s", str(item.get("claim"))[:60])
            continue
        try:
            out.append(FactEvent(claim=item["claim"], date=item["date"],
                                 source=item["source"], url=url))
        except (KeyError, ValidationError) as exc:
            logger.info("召回層線索欄位不全，丟棄：%s", exc)
    return out


def fetch_facts(date_str: str) -> tuple[FactsPack, dict[str, int]]:
    """用 Gemini + google_search 抓近兩日市場重大事件，回 (FactsPack, usage)。

    模型走 **flash 而非 pro**：純檢索不需要 pro 的推理力，省一半成本。
    任何失敗都回空 FactsPack（不 raise）——召回層是加值資訊，不該讓晨報整個掛掉；
    決策層拿不到 facts 時仍可只憑 features 產出晨報。
    """
    prompt = (
        f"{_RETRIEVAL_RULES}\n\n"
        f"請用 Google 搜尋，列出 {date_str} 前後最近兩天內、會影響台股／美股／加密貨幣的重大事件"
        f"（總經數據、央行/利率、政策、地緣政治、重大企業或產業消息）。\n"
        f"只輸出符合 schema 的 JSON。"
    )
    try:
        raw, usage, candidate = _generate_with_sources(prompt)
    except LLMError as exc:
        logger.warning("召回層抓取失敗（晨報改為只憑 features 產出）：%s", exc)
        return FactsPack(), {}

    events = _attach_urls(raw.get("events") or [], _all_grounding_urls(candidate))
    logger.info("召回層取得 %d 則待查證線索（date=%s）", len(events), date_str)
    return FactsPack(events=events), usage


def _generate_with_sources(prompt: str) -> tuple[dict[str, Any], dict[str, int], dict]:
    """和 `gemini_client._generate_json` 相同，但額外把 candidate 原樣回傳給我們取 grounding。

    Gemini 的 responseSchema 不能與 tools 並用，所以這裡**不帶 schema**、改用純文字 +
    「只輸出 JSON」的指示，再自己 parse——這正是 Gemini 路徑那個兩段式的成因。
    """
    text, usage, candidate = gemini_client.generate_text_with_candidate(
        prompt, model=settings.gemini_model_qa, use_search=True,
    )
    cleaned = text.strip()
    # 模型偶爾會把 JSON 包在 ```json fence 裡
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMError(f"召回層回的不是合法 JSON: {exc}; head={cleaned[:200]}") from exc
    if not isinstance(payload, dict):
        raise LLMError("召回層回的 JSON 不是物件")
    return payload, usage, candidate
