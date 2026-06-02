"""每日晨報排程 + catch-up（M3，對齊 design_docs §13 / ARCHITECTURE §M3）。

設計取捨：scheduler 不重做 backend 的工作，只在 08:30（Asia/Taipei）透過 HTTP 觸發
backend `POST /brief/morning`（該端點會刷新台股/美股/加密資料 + 產報告）。家用 PC 8:30
可能沒開機，故啟動時做 catch-up：若「已過今日排程時間且今日尚無報告」就立即補產。

不加 always-on 外部觸發（ARCHITECTURE §49）；Discord 推播留待 M4。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("ai-market-scheduler")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")
SCHEDULE_TZ = os.environ.get("SCHEDULE_TZ", "Asia/Taipei")
MORNING_REPORT_TIME = os.environ.get("MORNING_REPORT_TIME", "08:30")
# 整條管線（刷新台股/美股/加密 + Gemini）可能跑數分鐘，給足 read timeout
GENERATE_TIMEOUT = float(os.environ.get("BRIEF_GENERATE_TIMEOUT", "900"))

TZ = ZoneInfo(SCHEDULE_TZ)


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def generate_brief(*, reason: str = "scheduled") -> None:
    logger.info("觸發晨報產生 (%s) → POST %s/brief/morning", reason, BACKEND_URL)
    try:
        resp = httpx.post(f"{BACKEND_URL}/brief/morning", timeout=GENERATE_TIMEOUT)
        if resp.status_code == 200:
            body = resp.json()
            logger.info("晨報產生完成: report_id=%s url=%s",
                        body.get("report_id"), body.get("url"))
        else:
            logger.error("晨報產生失敗 HTTP %s: %s", resp.status_code, resp.text[:300])
    except Exception as exc:  # noqa: BLE001 — 排程不可因單次失敗而中止
        logger.error("晨報產生請求例外: %s", exc)


def _wait_backend_ready(max_wait: float = 120.0) -> bool:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{BACKEND_URL}/health", timeout=5).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    return False


def catch_up() -> None:
    """啟動補產：已過今日排程時間且今日尚無報告 → 立即補產。"""
    if not _wait_backend_ready():
        logger.warning("backend 未就緒，跳過 catch-up（等下次排程）")
        return
    try:
        st = httpx.get(f"{BACKEND_URL}/brief/status", timeout=30).json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("查 /brief/status 失敗，跳過 catch-up: %s", exc)
        return

    h, m = _parse_hhmm(MORNING_REPORT_TIME)
    now = datetime.now(TZ)
    scheduled_today = now.replace(hour=h, minute=m, second=0, microsecond=0)
    has_today = bool(st.get("has_today"))

    if not has_today and now >= scheduled_today:
        logger.info("catch-up：今日(%s)尚無報告且已過 %s，補產生",
                    st.get("schedule_date"), MORNING_REPORT_TIME)
        generate_brief(reason="catch-up")
    else:
        logger.info("catch-up：無需補產（has_today=%s, 現在=%s, 排程=%s）",
                    has_today, now.strftime("%H:%M"), MORNING_REPORT_TIME)


def main() -> None:
    h, m = _parse_hhmm(MORNING_REPORT_TIME)
    logger.info("scheduler 啟動：每日 %02d:%02d %s 產生晨報，backend=%s",
                h, m, SCHEDULE_TZ, BACKEND_URL)

    catch_up()

    scheduler = BlockingScheduler(timezone=TZ)
    scheduler.add_job(
        generate_brief,
        CronTrigger(hour=h, minute=m, timezone=TZ),
        id="morning_brief",
        misfire_grace_time=3600,  # 排程點剛好錯過（重啟）容忍 1 小時內補跑
        coalesce=True,
        max_instances=1,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler 結束")


if __name__ == "__main__":
    main()
