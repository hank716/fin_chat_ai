"""Storage 容量端點（M2 驗收：storage_monitor 回報 footprint vs budget）。"""

from __future__ import annotations

from fastapi import APIRouter

from storage.storage_monitor import local_storage_report

router = APIRouter(tags=["storage"])


@router.get("/storage")
async def storage() -> dict:
    return local_storage_report()
