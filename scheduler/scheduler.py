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
# 多時間點排程：REPORT_TIMES 為逗號分隔 HH:MM（如 "08:30,14:00,21:30"）。
# 向後相容：未設 REPORT_TIMES 時沿用舊的 MORNING_REPORT_TIME（預設 08:30）。
REPORT_TIMES = os.environ.get("REPORT_TIMES", os.environ.get("MORNING_REPORT_TIME", "08:30"))
# （可選）焦點股基本面預抓時間點：逗號分隔 HH:MM，留空＝關閉。設在 REPORT_TIMES 之前可讓晨報更快、
# 分散 FinMind 用量。例：PREFETCH_TIMES=07:30（07:30 暖快取，08:30 產報告直接讀磁碟）。
PREFETCH_TIMES = os.environ.get("PREFETCH_TIMES", "")
# （可選）全市場財報「慢爬」時間點：逗號分隔 HH:MM，留空＝關閉。觸發後背景慢慢掃全市場(2700+)
# 財報到磁碟（焦點股優先），可數小時～23h；季更慢資料，靠快取 TTL 可重入續跑。例：CRAWL_TIMES=09:00。
CRAWL_TIMES = os.environ.get("CRAWL_TIMES", "")
# 整條管線（刷新台股/美股/加密 + Gemini）可能跑數分鐘，給足 read timeout
GENERATE_TIMEOUT = float(os.environ.get("BRIEF_GENERATE_TIMEOUT", "900"))

TZ = ZoneInfo(SCHEDULE_TZ)


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


def _parse_times(s: str) -> list[tuple[int, int]]:
    """解析逗號分隔的 HH:MM 清單 → 已排序去重的 (h, m)。全空則回 [(8, 30)]。"""
    out: list[tuple[int, int]] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(_parse_hhmm(part))
        except ValueError:
            logger.warning("忽略無法解析的排程時間: %r", part)
    return sorted(set(out)) or [(8, 30)]


def _is_trading_day() -> bool:
    """問 backend 今天是否台股交易日（非交易日不產報告）。查詢失敗時保守視為交易日。"""
    try:
        st = httpx.get(f"{BACKEND_URL}/brief/status", timeout=15).json()
        return bool(st.get("is_trading_day", True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("查交易日失敗，保守視為交易日: %s", exc)
        return True


def generate_brief(*, reason: str = "scheduled") -> None:
    if reason == "scheduled" and not _is_trading_day():
        logger.info("今日非台股交易日，略過晨報")
        return
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


def prefetch_fundamentals(*, reason: str = "scheduled") -> None:
    """（可選）觸發 backend 預抓 watchlist 基本面到磁碟快取。僅交易日跑。"""
    if reason == "scheduled" and not _is_trading_day():
        logger.info("今日非台股交易日，略過基本面預抓")
        return
    logger.info("觸發基本面預抓 (%s) → POST %s/brief/prefetch", reason, BACKEND_URL)
    try:
        resp = httpx.post(f"{BACKEND_URL}/brief/prefetch", timeout=GENERATE_TIMEOUT)
        if resp.status_code == 200:
            logger.info("基本面預抓完成: %s", resp.json())
        else:
            logger.error("基本面預抓失敗 HTTP %s: %s", resp.status_code, resp.text[:300])
    except Exception as exc:  # noqa: BLE001
        logger.error("基本面預抓請求例外: %s", exc)


def crawl_fundamentals(*, reason: str = "scheduled") -> None:
    """（可選）觸發 backend 背景慢爬全市場財報（焦點股優先）。僅交易日跑；立刻回（背景執行）。"""
    if reason == "scheduled" and not _is_trading_day():
        logger.info("今日非台股交易日，略過全市場財報慢爬")
        return
    logger.info("觸發全市場財報慢爬 (%s) → POST %s/brief/prefetch?scope=full", reason, BACKEND_URL)
    try:
        resp = httpx.post(f"{BACKEND_URL}/brief/prefetch", params={"scope": "full"}, timeout=30)
        logger.info("全市場財報慢爬已啟動: HTTP %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        logger.error("全市場財報慢爬觸發例外: %s", exc)


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

    # 以「最早的排程時間」當作今日是否該有報告的門檻
    h, m = _parse_times(REPORT_TIMES)[0]
    now = datetime.now(TZ)
    scheduled_today = now.replace(hour=h, minute=m, second=0, microsecond=0)
    has_today = bool(st.get("has_today"))
    is_trading = bool(st.get("is_trading_day", True))

    if not is_trading:
        logger.info("catch-up：今日(%s)非台股交易日，不補產", st.get("schedule_date"))
    elif not has_today and now >= scheduled_today:
        logger.info("catch-up：今日(%s)尚無報告且已過最早排程 %02d:%02d，補產生",
                    st.get("schedule_date"), h, m)
        generate_brief(reason="catch-up")
    else:
        logger.info("catch-up：無需補產（trading=%s, has_today=%s, 現在=%s, 最早排程=%02d:%02d）",
                    is_trading, has_today, now.strftime("%H:%M"), h, m)


def main() -> None:
    times = _parse_times(REPORT_TIMES)
    logger.info("scheduler 啟動：每日 %s（%s）產生報告，backend=%s",
                ", ".join(f"{h:02d}:{m:02d}" for h, m in times), SCHEDULE_TZ, BACKEND_URL)

    catch_up()

    scheduler = BlockingScheduler(timezone=TZ)
    for h, m in times:
        scheduler.add_job(
            generate_brief,
            CronTrigger(hour=h, minute=m, timezone=TZ),
            id=f"brief_{h:02d}{m:02d}",
            misfire_grace_time=3600,  # 排程點剛好錯過（重啟）容忍 1 小時內補跑
            coalesce=True,
            max_instances=1,
        )

    prefetch_times = _parse_times(PREFETCH_TIMES) if PREFETCH_TIMES.strip() else []
    for h, m in prefetch_times:
        scheduler.add_job(
            prefetch_fundamentals,
            CronTrigger(hour=h, minute=m, timezone=TZ),
            id=f"prefetch_{h:02d}{m:02d}",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
    if prefetch_times:
        logger.info("基本面預抓排程：每日 %s",
                    ", ".join(f"{h:02d}:{m:02d}" for h, m in prefetch_times))

    crawl_times = _parse_times(CRAWL_TIMES) if CRAWL_TIMES.strip() else []
    for h, m in crawl_times:
        scheduler.add_job(
            crawl_fundamentals,
            CronTrigger(hour=h, minute=m, timezone=TZ),
            id=f"crawl_{h:02d}{m:02d}",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
    if crawl_times:
        logger.info("全市場財報慢爬排程：每日 %s",
                    ", ".join(f"{h:02d}:{m:02d}" for h, m in crawl_times))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler 結束")


if __name__ == "__main__":
    main()
