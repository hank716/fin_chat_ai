"""Storage 容量端點（M2 驗收：storage_monitor 回報 footprint vs budget）。"""

from __future__ import annotations

from fastapi import APIRouter

from storage.storage_monitor import local_storage_report

router = APIRouter(tags=["storage"])


@router.get("/storage")
async def storage() -> dict:
    return local_storage_report()


@router.post("/storage/retention")
async def retention() -> dict:
    """手動觸發保留/清理（舊報告汰換 + 清單外 parquet 快取清理）。"""
    from storage.retention import enforce_retention

    return enforce_retention()
