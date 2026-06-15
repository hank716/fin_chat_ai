"""系統活動監測 + 待機時段建議。

目的：使用者想知道「電腦何時可以待機省電」。本模組只計**本系統的對外工作**——所有經
rate_limiter 的資料抓取（FinMind/TWSE/TPEx/yahoo，含晨報、預抓、全市場慢爬）與每次 Gemini 呼叫
（晨報/問答/意圖分類）。Redis keep-alive、cloudflared tunnel 等閒置長連線不算。

資料結構（Redis）：每天一個 hash `activity:day:{YYYYMMDD}`，field=當地時區「當日分鐘索引」(0–1439)，
value=該分鐘的活動次數；hash 設 ~35 天 TTL 自然汰換。累積數日後，對每個 minute-of-day 統計「有活動
的天數佔比」，找出長期都低於門檻的連續時段＝建議待機窗。

埋點絕不可影響主流程：所有 Redis 例外都吞掉；同一分鐘同一來源用行程內去重，避免熱路徑狂打 Redis。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import settings
from redis_client import redis_client

logger = logging.getLogger("ai-market-backend.activity")

RETENTION_DAYS = 35                      # day-hash 保留天數
_DAY_TTL = RETENTION_DAYS * 24 * 3600
MINUTES_PER_DAY = 24 * 60

# 行程內去重：每個 source 記住「上次標記的 wall-clock 分鐘」，同分鐘只寫一次。
_last_marked: dict[str, int] = {}


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.schedule_tz))


def _day_key(d: datetime) -> str:
    return f"activity:day:{d:%Y%m%d}"


def mark(source: str = "data") -> None:
    """記一次本系統活動（source 例：'data'＝資料抓取、'ai'＝Gemini 呼叫、'compute'＝本機回測/模型訓練）。

    多數來源是對外工作；'compute' 為本機 CPU 工作（回測/edge 模型訓練），一併計入是為了讓
    『待機建議』在訓練進行時不要建議讓電腦睡。寫入「當地時區當日分鐘索引」的計數；
    同一來源同一分鐘只寫一次。任何例外都靜默忽略。
    """
    try:
        epoch_min = int(time.time() // 60)
        if _last_marked.get(source) == epoch_min:
            return
        _last_marked[source] = epoch_min
        now = _now()
        minute_of_day = now.hour * 60 + now.minute
        key = _day_key(now)
        redis_client.hincrby(key, str(minute_of_day), 1)
        redis_client.expire(key, _DAY_TTL)
    except Exception as exc:  # noqa: BLE001 — 監測絕不可拖垮主流程
        logger.debug("activity.mark 失敗（忽略）: %s", exc)


def _active_minutes(d: datetime) -> set[int] | None:
    """回傳該日有活動的分鐘索引集合；該日完全無資料（key 不存在）回 None（PC 當天可能沒開機）。"""
    try:
        h = redis_client.hgetall(_day_key(d))
    except Exception as exc:  # noqa: BLE001
        logger.debug("讀 activity day 失敗: %s", exc)
        return None
    if not h:
        return None
    out: set[int] = set()
    for k, v in h.items():
        try:
            if int(v) > 0:
                out.add(int(k))
        except (TypeError, ValueError):
            continue
    return out


def _windows_from_idle(idle_minutes: list[bool], min_len: int) -> list[dict]:
    """把「每分鐘是否閒置」的布林序列轉成連續閒置區段（>= min_len 分鐘才列入）。"""
    windows: list[dict] = []
    start: int | None = None
    for i in range(MINUTES_PER_DAY):
        if idle_minutes[i]:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= min_len:
                windows.append({"start": _fmt(start), "end": _fmt(i), "minutes": i - start})
            start = None
    if start is not None and MINUTES_PER_DAY - start >= min_len:
        windows.append(
            {"start": _fmt(start), "end": _fmt(MINUTES_PER_DAY), "minutes": MINUTES_PER_DAY - start}
        )
    return windows


def _fmt(minute_of_day: int) -> str:
    m = min(minute_of_day, MINUTES_PER_DAY)  # 1440 → 顯示 24:00
    return f"{m // 60:02d}:{m % 60:02d}"


def idle_report(
    days: int = 14,
    *,
    idle_threshold: float = 0.15,
    min_window_min: int = 30,
    min_observed_days: int = 3,
) -> dict:
    """彙整最近 days 天的活動，回每小時熱度 + 建議待機窗 + 已累積天數。

    - observed_days：最近 days 天中「有開機/有記錄」的天數（key 存在者），當分母。
    - 每個 minute-of-day 的 active_fraction = 該分鐘有活動的天數 / observed_days。
    - 建議待機窗：active_fraction <= idle_threshold 且連續 >= min_window_min 分鐘的時段。
    - hourly：24 桶，每桶為該小時 60 分鐘 active_fraction 的平均（0–1，給網頁畫熱度條）。
    """
    today = _now()
    day_sets: list[set[int]] = []
    for i in range(days):
        s = _active_minutes(today - timedelta(days=i))
        if s is not None:
            day_sets.append(s)

    observed_days = len(day_sets)
    enough = observed_days >= min_observed_days

    # 每分鐘的活動天數佔比
    active_count = [0] * MINUTES_PER_DAY
    for s in day_sets:
        for minute in s:
            if 0 <= minute < MINUTES_PER_DAY:
                active_count[minute] += 1
    fraction = [
        (active_count[i] / observed_days if observed_days else 0.0)
        for i in range(MINUTES_PER_DAY)
    ]

    hourly = [
        round(sum(fraction[h * 60:(h + 1) * 60]) / 60.0, 4)
        for h in range(24)
    ]
    idle_minutes = [fraction[i] <= idle_threshold for i in range(MINUTES_PER_DAY)]
    idle_windows = _windows_from_idle(idle_minutes, min_window_min) if enough else []

    return {
        "observed_days": observed_days,
        "min_observed_days": min_observed_days,
        "enough_data": enough,
        "idle_threshold": idle_threshold,
        "hourly": hourly,            # 24 個 0–1 浮點（活動強度）
        "idle_windows": idle_windows,
        "tz": settings.schedule_tz,
    }
