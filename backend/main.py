"""FastAPI 進入點（M0 垂直骨架）。

啟動時建立 Redis 連線並掛在 app.state，供 /health 與後續里程碑使用。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI

from api.health import router as health_router
from config import settings

logging.basicConfig(level=settings.log_level.upper())
logger = logging.getLogger("ai-market-backend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True)
    logger.info("backend 啟動，redis=%s env=%s", settings.redis_url, settings.app_env)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        logger.info("backend 關閉")


app = FastAPI(title="AI 多市場研究助理", version="0.0.1-m0", lifespan=lifespan)
app.include_router(health_router)


@app.get("/")
async def root() -> dict:
    return {"service": "ai-market-backend", "docs": "/docs", "health": "/health"}
