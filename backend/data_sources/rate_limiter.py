"""Redis-based token bucket rate limiter（移植 finflow_ai/services/rate_limiter.py）。

每個 provider (finmind / twse / tpex / yahoo) 一個 bucket。
用 Redis 而非 in-memory：worker / scheduler / api 是不同 process，
共享 token state 才能正確 throttle 整個系統的對外 API 用量。

移植變更：redis key prefix finflow → finchat（避免與舊專案共用 Redis 時撞 key）。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

import redis

from activity import monitor
from config import settings
from redis_client import redis_client


@dataclass(frozen=True)
class Quota:
    """每秒可用 token 上限 + bucket 容量上限 + 每小時硬上限（0＝不限）。

    rate_per_sec/burst 管「瞬時平滑度」，hourly_budget 管「整點額度」。兩者互補：
    前者防瞬間打爆，後者防一小時內累計超過 provider 的 per-hour 配額（FinMind 600/h）。
    """
    rate_per_sec: float
    burst: int
    hourly_budget: int = 0


# 各 provider 的保守上限（FinMind 免費 600 req/h；TWSE/TPEx 不可暴力掃）
QUOTAS: dict[str, Quota] = {
    # FinMind 0.5/s：給延遲敏感的互動路徑（一次冷快取晨報 ~225 calls，0.5/s 約 7~8 分、
    # 單次仍 < 600/h）留速度。防「整點額度爆掉被封 IP」靠結構面而非壓死此速率：
    #   1) 全市場初始化改用 backfill_tw_market（走 TWSE/TPEx，完全不打 FinMind）
    #   2) 跨 process 節流已修正（_try_consume 用 wall clock，docker exec 與 backend 共享同桶）
    #   3) 全市場財報慢爬 reentrant 且容忍 403（FinMindIPBanned 被逐檔 except 接住、可續跑）
    # burst 30：讓熱快取晨報的 ~24 則新聞請求瞬間完成、不被逐筆節流。
    "finmind": Quota(rate_per_sec=0.5, burst=30, hourly_budget=settings.finmind_hourly_budget),
    # TWSE/TPEx 官方端點有反爬 WAF：持續 1 req/s 連打數分鐘（歷史慢爬）會被回 HTTP 307
    # 「安全性考量」暫時封鎖。降到 0.4/s + 較小 burst，並在 acquire 等待時加 jitter（見下），
    # 打散規律高頻、避開 WAF pattern。每日刷新只抓近 3 天、量小，降頻不影響晨報速度。
    "twse": Quota(rate_per_sec=0.4, burst=3),
    "tpex": Quota(rate_per_sec=0.4, burst=3),
    "yahoo": Quota(rate_per_sec=2.0, burst=10),
}


class RateLimitTimeout(RuntimeError):
    pass


class RateLimitExhausted(RuntimeError):
    """provider 當小時硬上限已用罄（非瞬時節流，而是整點配額）。

    與 RateLimitTimeout 不同：等下去也沒用（要等到下個整點才回補），呼叫端應退避而非重試。
    """

    def __init__(self, provider: str, budget: int) -> None:
        self.provider = provider
        self.budget = budget
        super().__init__(f"{provider} hourly budget {budget} exhausted")


def _key(provider: str) -> str:
    return f"finchat:ratelimit:{provider}"


def _hourly_key(provider: str) -> str:
    # 以 epoch 整點分桶；跨 process 共享同一桶，TTL 自然汰換。
    return f"finchat:ratelimit:{provider}:h{int(time.time() // 3600)}"


def _check_hourly_budget(cli: redis.Redis, provider: str, quota: Quota, cost: int) -> None:
    """整點配額檢查：超過 hourly_budget 抛 RateLimitExhausted。

    用 INCRBY 計數（跨 process 一致）；若超量就把這次的增量退回，避免永久灌大計數。
    Redis 故障時放行（寧可少擋，也不要因為快取層掛掉而擋住所有對外請求）。
    """
    if quota.hourly_budget <= 0:
        return
    key = _hourly_key(provider)
    try:
        used = cli.incrby(key, cost)
        if used == cost:  # 本小時第一次，設定過期（留 2h 容跨桶讀取）
            cli.expire(key, 7200)
    except redis.RedisError:
        return
    if used > quota.hourly_budget:
        try:
            cli.decrby(key, cost)
        except redis.RedisError:
            pass
        raise RateLimitExhausted(provider, quota.hourly_budget)


def acquire(provider: str, *, cost: int = 1, max_wait_sec: float = 30.0,
            client: redis.Redis | None = None) -> float:
    """阻塞直到拿到 token, 回傳實際 wait 秒數。

    超過 max_wait_sec 抛 RateLimitTimeout；當小時硬上限用罄抛 RateLimitExhausted。
    cost: 一次操作消耗幾個 token (例如批次抓 100 檔可給 cost=10)。
    """
    cli = client or redis_client
    quota = QUOTAS.get(provider)
    if not quota:
        # 未知 provider 不 throttle, 讓上游決定
        return 0.0

    # 先檢查整點配額（便宜、不睡）：用罄就直接抛，免得白等 token 後仍失敗。
    _check_hourly_budget(cli, provider, quota, cost)

    deadline = time.monotonic() + max_wait_sec
    waited = 0.0
    while True:
        if _try_consume(cli, provider, cost, quota):
            monitor.mark("data")  # 記一次本系統對外資料抓取（待機偵測用；同分鐘去重）
            return waited
        if time.monotonic() >= deadline:
            raise RateLimitTimeout(f"{provider} rate limit timeout (waited {waited:.1f}s)")
        # 估計需 wait 多久才能補滿 cost 個 token；加 jitter 打散規律高頻（避開反爬 WAF pattern）。
        sleep_sec = max(cost / quota.rate_per_sec, 0.1) * random.uniform(0.8, 1.5)
        time.sleep(sleep_sec)
        waited += sleep_sec


def _try_consume(cli: redis.Redis, provider: str, cost: int, quota: Quota) -> bool:
    """Lua-less token bucket: 用 hash + WATCH/MULTI optimistic concurrency。

    不用 Lua：簡單性。MVP 流量低, 不是熱路徑。
    """
    key = _key(provider)
    # 用 wall clock (time.time) 而非 monotonic：last 時間戳存在「跨 process 共享」的
    # Redis，monotonic 的原點每個 process／每次重啟都不同，跨容器讀回來的差值無意義
    # → 等於沒在 throttle（多容器一起灌就爆額度）。wall clock 全機一致，可正確補 token。
    now = time.time()
    with cli.pipeline() as pipe:
        for _ in range(3):  # 重試 race
            try:
                pipe.watch(key)
                state = cli.hgetall(key)
                tokens = float(state.get("tokens", quota.burst))
                last = float(state.get("last", now))
                # 補 token（max(0, ..) 防 NTP 校時/時鐘回退把 elapsed 算成負數而扣 token）
                elapsed = max(0.0, now - last)
                tokens = min(quota.burst, tokens + elapsed * quota.rate_per_sec)
                if tokens < cost:
                    pipe.unwatch()
                    return False
                pipe.multi()
                pipe.hset(key, mapping={"tokens": tokens - cost, "last": now})
                pipe.expire(key, 3600)
                pipe.execute()
                return True
            except redis.WatchError:
                continue
    return False
