"""Redis-based token bucket rate limiter（移植 finflow_ai/services/rate_limiter.py）。

每個 provider (finmind / twse / tpex / yahoo) 一個 bucket。
用 Redis 而非 in-memory：worker / scheduler / api 是不同 process，
共享 token state 才能正確 throttle 整個系統的對外 API 用量。

移植變更：redis key prefix finflow → finchat（避免與舊專案共用 Redis 時撞 key）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import redis

from redis_client import redis_client


@dataclass(frozen=True)
class Quota:
    """每秒可用 token 上限 + bucket 容量上限。"""
    rate_per_sec: float
    burst: int


# 各 provider 的保守上限（FinMind 免費 600 req/h；TWSE/TPEx 不可暴力掃）
QUOTAS: dict[str, Quota] = {
    # FinMind 0.5/s：給延遲敏感的互動路徑（一次冷快取晨報 ~225 calls，0.5/s 約 7~8 分、
    # 單次仍 < 600/h）留速度。防「整點額度爆掉被封 IP」靠結構面而非壓死此速率：
    #   1) 全市場初始化改用 backfill_tw_market（走 TWSE/TPEx，完全不打 FinMind）
    #   2) 跨 process 節流已修正（_try_consume 用 wall clock，docker exec 與 backend 共享同桶）
    #   3) 全市場財報慢爬 reentrant 且容忍 403（FinMindIPBanned 被逐檔 except 接住、可續跑）
    # burst 30：讓熱快取晨報的 ~24 則新聞請求瞬間完成、不被逐筆節流。
    "finmind": Quota(rate_per_sec=0.5, burst=30),
    "twse": Quota(rate_per_sec=1.0, burst=5),
    "tpex": Quota(rate_per_sec=1.0, burst=5),
    "yahoo": Quota(rate_per_sec=2.0, burst=10),
}


class RateLimitTimeout(RuntimeError):
    pass


def _key(provider: str) -> str:
    return f"finchat:ratelimit:{provider}"


def acquire(provider: str, *, cost: int = 1, max_wait_sec: float = 30.0,
            client: redis.Redis | None = None) -> float:
    """阻塞直到拿到 token, 回傳實際 wait 秒數。

    超過 max_wait_sec 抛 RateLimitTimeout。
    cost: 一次操作消耗幾個 token (例如批次抓 100 檔可給 cost=10)。
    """
    cli = client or redis_client
    quota = QUOTAS.get(provider)
    if not quota:
        # 未知 provider 不 throttle, 讓上游決定
        return 0.0

    deadline = time.monotonic() + max_wait_sec
    waited = 0.0
    while True:
        if _try_consume(cli, provider, cost, quota):
            return waited
        if time.monotonic() >= deadline:
            raise RateLimitTimeout(f"{provider} rate limit timeout (waited {waited:.1f}s)")
        # 估計需 wait 多久才能補滿 cost 個 token
        sleep_sec = max(cost / quota.rate_per_sec, 0.1)
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
