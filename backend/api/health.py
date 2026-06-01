"""Health endpoint（M0 驗收：GET /health 回 200）。

Liveness 永遠 200；附帶 Redis 連線狀態作為 readiness 訊號。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    redis_status = "down"
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        try:
            if await redis.ping():
                redis_status = "up"
        except Exception:  # noqa: BLE001 — health 不應因依賴掛掉而 5xx
            redis_status = "down"

    return {
        "status": "ok",
        "service": "ai-market-backend",
        "redis": redis_status,
        "time": datetime.now(timezone.utc).isoformat(),
    }
