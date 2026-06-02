"""pCloud 冷儲存備份（M6，對齊 design_docs §9.3/§23）。

把產好的晨報 json/md 上傳到 pCloud {PCLOUD_REMOTE_ROOT}/backups/reports/。
沿用 finflow 憑證但用全新 root（/AI-Market-Research）去衝突。失敗只記 log，不阻斷產報告。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from config import settings

logger = logging.getLogger("ai-market-backend.publish.pcloud")

TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _base() -> str:
    return "https://eapi.pcloud.com" if settings.pcloud_region == "eu" else "https://api.pcloud.com"


def _remote_folder() -> str:
    root = settings.pcloud_remote_root.rstrip("/")
    return f"{root}/backups/reports"


def _ensure_folder() -> int | None:
    """確保 {root}/backups/reports 存在，回 folderid。

    pCloud createfolderifnotexists 不會遞迴建父層（缺父層回 result=2002），
    故逐層建立每個路徑元件。
    """
    parts = [p for p in _remote_folder().split("/") if p]
    folderid: int | None = None
    cur = ""
    try:
        for part in parts:
            cur = f"{cur}/{part}"
            r = httpx.get(
                f"{_base()}/createfolderifnotexists",
                params={"access_token": settings.pcloud_access_token, "path": cur},
                timeout=TIMEOUT,
            )
            j = r.json()
            if j.get("result") != 0:
                logger.error("pCloud 建資料夾 %s 失敗: result=%s %s",
                             cur, j.get("result"), j.get("error"))
                return None
            folderid = j["metadata"]["folderid"]
        return folderid
    except Exception as exc:  # noqa: BLE001
        logger.error("pCloud 建資料夾例外: %s", exc)
    return None


def _upload(folderid: int, path: Path) -> str | None:
    try:
        r = httpx.post(
            f"{_base()}/uploadfile",
            params={"access_token": settings.pcloud_access_token, "folderid": folderid,
                    "nopartial": 1},
            files={path.name: (path.name, path.read_bytes())},
            timeout=TIMEOUT,
        )
        j = r.json()
        if j.get("result") == 0 and j.get("metadata"):
            return f"{_remote_folder()}/{path.name}"
        logger.error("pCloud 上傳 %s 失敗: result=%s %s", path.name, j.get("result"), j.get("error"))
    except Exception as exc:  # noqa: BLE001
        logger.error("pCloud 上傳 %s 例外: %s", path.name, exc)
    return None


def backup_report(json_path: Path, md_path: Path) -> dict[str, Any]:
    """上傳 json + md 到 pCloud，回 {pcloud_json_path, pcloud_markdown_path}（失敗為 None）。"""
    if not settings.pcloud_access_token.strip():
        logger.warning("PCLOUD_ACCESS_TOKEN 未設定，略過備份")
        return {}
    folderid = _ensure_folder()
    if folderid is None:
        return {}
    result = {
        "pcloud_json_path": _upload(folderid, json_path),
        "pcloud_markdown_path": _upload(folderid, md_path),
    }
    logger.info("pCloud 備份完成: %s", result)
    return result
