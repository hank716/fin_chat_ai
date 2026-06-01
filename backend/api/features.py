"""Features 端點（M1 step 2 驗證 + 後續 step 取用）。"""

from __future__ import annotations

from fastapi import APIRouter

from processor.intermarket_features import build_intermarket_features

router = APIRouter(tags=["features"])


@router.get("/features/intermarket")
async def intermarket(window: int = 20) -> dict:
    return build_intermarket_features(window=window)
