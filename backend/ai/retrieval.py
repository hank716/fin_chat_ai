"""廣度召回層 — Gemini + google_search 產「待查證線索」（spec 022-llm-tiering）。

憲章 2.0.0 Principle I：此層**只負責找得到**，不做分析、方向判斷或選股。輸出是帶 source URL
的事實線索（facts pack），交給決策層逐條查證後才採信。

為什麼要這樣切：Gemini 的 grounding 會產生幻覺——錯置日期、把分析評論當成新聞、引用內容
農場。由同一個模型同時召回與決策時，沒有任何一方能稽核另一方，幻覺會被洗成「有來源」的
假事實直接寫進晨報。分層後，決策層用 `web_fetch` 開啟這裡給的 URL 核對，兩者互為對照。

**無來源的線索一律丟棄**——查證不了的東西不該進決策層。

⚠️ Gemini 給的來源是 `vertexaisearch.cloud.google.com/grounding-api-redirect/...` 轉址，
而決策層的 `web_fetch` 對該網域回 `url_not_allowed`——不先解析轉址，整個查證閉環就是
空轉（fetch 次次失敗卻照樣佔用 max_uses）。見 `_resolve_redirect`。
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


_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
_RESOLVE_TIMEOUT = 6.0


def _resolve_redirect(url: str) -> str:
    """把 Google grounding 的轉址網址換成真正的文章網址。

    ⚠️ 這是查證閉環的**必要**步驟，不是優化。Gemini 的 groundingChunks 給的是
    `https://vertexaisearch.cloud.google.com/grounding-api-redirect/<opaque>`，而決策層的
    `web_fetch` 對這個網域一律回 **`url_not_allowed`**——2026-08-07 那篇 7 條 fact_checks
    全部 `unverifiable`、note 幾乎都寫著 url_not_allowed，就是這個原因。也就是說：
    查證層從上線第一天起就結構性失效，每一次 fetch 都注定失敗卻照樣計費。

    轉址解析在本地做（HEAD + follow_redirects），零 LLM 成本；解不開就沿用原網址——
    決策層仍會誠實地標成 unverifiable，不會因為我們解析失敗就把它當成已查證。
    """
    if _REDIRECT_HOST not in url:
        return url
    try:
        import httpx

        with httpx.Client(follow_redirects=True, timeout=_RESOLVE_TIMEOUT) as client:
            resp = client.head(url)
            # 少數站台不吃 HEAD（405/501）→ 退回 GET，但只讀 headers 就關掉，不下載內容
            if resp.status_code >= 400:
                with client.stream("GET", url) as stream_resp:
                    final = str(stream_resp.url)
            else:
                final = str(resp.url)
        if final and _REDIRECT_HOST not in final:
            return final
        logger.info("轉址仍指向 grounding redirect，沿用原網址：%s", url[:80])
    except Exception as exc:  # noqa: BLE001 — 解不開不該讓整份晨報失敗
        logger.info("解析 grounding 轉址失敗（沿用原網址）：%s", exc)
    return url


def _attach_urls(events: list[dict[str, Any]], fallback_urls: list[str]) -> list[FactEvent]:
    """把 event 補上 URL 並丟掉補不到的。

    模型自己填的 url 優先；沒填就從 grounding 清單按序補（Gemini 常常在 groundingMetadata
    有來源、但沒把 url 寫進 JSON 欄位）。兩邊都沒有 → **丟棄**，因為查證不了。
    """
    out: list[FactEvent] = []
    spare = list(fallback_urls)
    resolved: dict[str, str] = {}      # 同一個轉址只解析一次（多則線索常共用同一來源）
    for item in events:
        url = (item.get("url") or "").strip()
        if not url and spare:
            url = spare.pop(0)
        if not url:
            logger.info("召回層線索無來源，丟棄：%s", str(item.get("claim"))[:60])
            continue
        if url not in resolved:
            resolved[url] = _resolve_redirect(url)
        url = resolved[url]
        try:
            out.append(FactEvent(claim=item["claim"], date=item["date"],
                                 source=item["source"], url=url))
        except (KeyError, ValidationError) as exc:
            logger.info("召回層線索欄位不全，丟棄：%s", exc)
    return out


def _model_chain() -> list[str]:
    """召回層的模型嘗試順序：便宜的先、pro 當最後手段。

    `gemini-flash-latest` 是最熱門的檔位，實測會整段回 503
    （"This model is currently experiencing high demand"），而同一組 tenacity 重試打的
    是**同一個過載模型**，退避再久也沒用——2026-08-06 的 E2E 就是 4 次全 503。
    換模型比多等有效得多，故失敗後改試別的檔位。
    """
    chain = [
        settings.gemini_model_qa,          # flash：預設，便宜且夠用
        settings.gemini_model_classifier,  # flash-lite：更便宜，通常較不壅塞
        settings.gemini_model_brief,       # pro：最後手段（貴，但檔位不同、較可能有容量）
    ]
    seen: set[str] = set()
    return [m for m in chain if m and not (m in seen or seen.add(m))]


def fetch_facts(date_str: str) -> tuple[FactsPack, dict[str, int]]:
    """用 Gemini + google_search 抓近兩日市場重大事件，回 (FactsPack, usage)。

    模型走 **flash 而非 pro**：純檢索不需要 pro 的推理力，省一半成本；flash 過載時
    依 `_model_chain()` 換檔位重試。全部失敗才回空 FactsPack（不 raise）——召回層是
    加值資訊，不該讓晨報整個掛掉；決策層拿不到 facts 時仍可只憑 features 產出晨報，
    但那次的查證閉環等於沒有被驗證到（report 的 fact_checks 會是空的）。
    """
    prompt = (
        f"{_RETRIEVAL_RULES}\n\n"
        f"請用 Google 搜尋，列出 {date_str} 前後最近兩天內、會影響台股／美股／加密貨幣的重大事件"
        f"（總經數據、央行/利率、政策、地緣政治、重大企業或產業消息）。\n"
        f"只輸出符合 schema 的 JSON。"
    )
    last_exc: LLMError | None = None
    for model in _model_chain():
        try:
            raw, usage, candidate = _generate_with_sources(prompt, model)
        except LLMError as exc:
            last_exc = exc
            logger.warning("召回層 %s 失敗，改試下一個檔位：%s", model, str(exc)[:160])
            continue
        events = _attach_urls(raw.get("events") or [], _all_grounding_urls(candidate))
        logger.info("召回層取得 %d 則待查證線索（date=%s model=%s）", len(events), date_str, model)
        return FactsPack(events=events), usage

    logger.warning("召回層所有檔位都失敗（晨報改為只憑 features 產出，本次無查證）：%s", last_exc)
    return FactsPack(), {}


def _generate_with_sources(
    prompt: str, model: str,
) -> tuple[dict[str, Any], dict[str, int], dict]:
    """和 `gemini_client._generate_json` 相同，但額外把 candidate 原樣回傳給我們取 grounding。

    Gemini 的 responseSchema 不能與 tools 並用，所以這裡**不帶 schema**、改用純文字 +
    「只輸出 JSON」的指示，再自己 parse——這正是 Gemini 路徑那個兩段式的成因。
    """
    text, usage, candidate = gemini_client.generate_text_with_candidate(
        prompt, model=model, use_search=True,
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
