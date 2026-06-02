"""Supabase report_index 發布（M6，對齊 design_docs §22.2）。

每日晨報產生後，把報告 metadata（標題/日期/路徑/pCloud 路徑/public_url）upsert 到
Supabase report_index（暖層索引，給歷史查詢用）。走 PostgREST + service_role key
（.env 無 DB 密碼，不直連 Postgres）。表需先以 sql/supabase_schema.sql 建好。

upsert 依 report_id（on_conflict）。失敗只記 log，不阻斷產報告。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings

logger = logging.getLogger("ai-market-backend.publish.supabase")

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
TABLE = "report_index"


def _public_url(report_id: str) -> str:
    base = (settings.public_report_base_url or "").rstrip("/")
    return f"{base}/report/{report_id}" if base else f"/report/{report_id}"


def publish_report_index(
    report: dict[str, Any],
    *,
    pcloud_paths: dict[str, Any] | None = None,
    discord_pushed: bool = False,
) -> bool:
    if not (settings.supabase_url.strip() and settings.supabase_service_role_key.strip()):
        logger.warning("Supabase 未設定，略過 report_index 發布")
        return False

    report_id = report.get("report_id", "")
    pcloud_paths = pcloud_paths or {}
    row = {
        "report_id": report_id,
        "report_type": report.get("report_type", "每日跨市場晨報"),
        "title": (report.get("headline", "") or "")[:300],
        "report_date": str(report.get("data_as_of") or "")[:10] or None,
        "data_as_of": str(report.get("data_as_of") or "")[:10] or None,
        "market_scope": ["美股", "加密貨幣", "台股"],
        "local_markdown_path": f"storage/reports/{report_id}.md",
        "local_json_path": f"storage/reports/{report_id}.json",
        "pcloud_markdown_path": pcloud_paths.get("pcloud_markdown_path"),
        "pcloud_json_path": pcloud_paths.get("pcloud_json_path"),
        "public_url": _public_url(report_id),
        "discord_pushed": discord_pushed,
    }
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        r = httpx.post(
            f"{settings.supabase_url}/rest/v1/{TABLE}",
            params={"on_conflict": "report_id"},
            headers=headers,
            json=row,
            timeout=TIMEOUT,
        )
        if r.status_code in (200, 201, 204):
            logger.info("Supabase report_index 發布成功: %s", report_id)
            return True
        if r.status_code == 404 or "PGRST205" in r.text:
            logger.error("Supabase report_index 表不存在 → 先跑 sql/supabase_schema.sql；resp=%s",
                         r.text[:160])
        else:
            logger.error("Supabase 發布失敗 HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        logger.error("Supabase 發布例外: %s", exc)
    return False
