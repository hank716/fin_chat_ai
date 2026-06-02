"""Discord REST 推播（M4，對齊 design §13.3 send_discord_summary）。

用 bot token 直接打 Discord REST API 發訊息到頻道，**不需** gateway 連線
（互動式 bot 才需 gateway，見 bot/ 服務）。每日晨報摘要由 backend 在產報告末段推送。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings
from reports.discord_summary import build_discord_summary

logger = logging.getLogger("ai-market-backend.notify.discord")

API = "https://discord.com/api/v10"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def send_message(channel_id: str, content: str) -> bool:
    """發一則訊息到指定頻道。回傳是否成功（失敗只記 log，不丟例外阻斷排程）。"""
    token = settings.discord_token.strip()
    if not token or not channel_id:
        logger.warning("Discord 未設定（token/channel 缺），略過推送")
        return False
    try:
        resp = httpx.post(
            f"{API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            json={"content": content},
            timeout=TIMEOUT,
        )
        if resp.status_code in (200, 201):
            return True
        logger.error("Discord 推送失敗 HTTP %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:  # noqa: BLE001 — 推送失敗不該炸掉產報告流程
        logger.error("Discord 推送例外: %s", exc)
        return False


def send_daily_summary(report: dict[str, Any]) -> bool:
    """把每日晨報短摘要推到 daily-report 頻道。"""
    summary = build_discord_summary(report)
    ok = send_message(settings.discord_daily_report_channel_id, summary)
    logger.info("每日晨報 Discord 推送 %s", "成功" if ok else "未送出")
    return ok
