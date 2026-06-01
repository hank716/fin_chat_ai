"""Sync Redis client（給抓取層 / rate_limiter / 排程 task 用）。

移植自 finflow_ai：worker / scheduler / api 不同 process 共享 token state，
才能正確 throttle 整個系統的對外 API 用量。FastAPI async 路由用 main.py 內
另開的 redis.asyncio client，與此同步 client 分開。
"""
from __future__ import annotations

import redis

from config import settings

redis_client: redis.Redis = redis.Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)


def check_redis() -> tuple[bool, str | None]:
    try:
        if redis_client.ping():
            return True, None
        return False, "ping returned falsy"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
