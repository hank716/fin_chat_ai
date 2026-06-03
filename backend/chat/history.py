"""Discord 討論串短期對話記憶（僅 thread 內有效）。

設計：只有在 Discord「討論串(thread)」裡問答才帶記憶；一般頻道訊息維持無狀態（省 token）。
每個討論串一條 redis list，存最近數輪 {q,a}，有 TTL，跨天自然汰換。資料事實仍以晨報/即時查詢為準，
歷史只給代名詞/追問脈絡（見 prompts.build_qa_prompt）。
"""
from __future__ import annotations

import json
import logging

from redis_client import redis_client

logger = logging.getLogger("ai-market-backend.chat")

MAX_TURNS = 6                      # prompt 帶入的最近輪數（一輪＝一問一答）
_TTL_SECONDS = 3 * 24 * 3600       # 討論串記憶保留 3 天
_Q_MAX, _A_MAX = 500, 1200         # 單則截斷，避免 prompt 無限膨脹


def _key(conversation_id: str) -> str:
    return f"chat:hist:{conversation_id}"


def load(conversation_id: str) -> list[dict[str, str]]:
    """取該討論串最近 MAX_TURNS 輪對話（由舊到新）。失敗回空（記憶是加分項，不可阻斷問答）。"""
    if not conversation_id:
        return []
    try:
        raw = redis_client.lrange(_key(conversation_id), -MAX_TURNS, -1)
        out: list[dict[str, str]] = []
        for item in raw:
            try:
                d = json.loads(item)
                out.append({"q": d.get("q", ""), "a": d.get("a", "")})
            except (json.JSONDecodeError, AttributeError):
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取討論串記憶失敗 %s: %s", conversation_id, exc)
        return []


def append(conversation_id: str, question: str, answer: str) -> None:
    """把這一輪問答寫入討論串記憶（截斷、限長度、續期 TTL）。"""
    if not conversation_id:
        return
    try:
        k = _key(conversation_id)
        redis_client.rpush(k, json.dumps(
            {"q": (question or "")[:_Q_MAX], "a": (answer or "")[:_A_MAX]},
            ensure_ascii=False,
        ))
        redis_client.ltrim(k, -MAX_TURNS, -1)   # 只留最近 MAX_TURNS 輪
        redis_client.expire(k, _TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("寫入討論串記憶失敗 %s: %s", conversation_id, exc)
