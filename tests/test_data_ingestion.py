"""spec 001 測試基線：token bucket 限流、整點配額、FinMind 402/403 退避、多來源 ingest 隔離。

額度保護壞掉的代價是 FinMind 封 IP 上千秒、晨報缺資料——所以每個擋法都要釘住。
全離線：redis、時鐘、HTTP 都換成替身。
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import redis

from data_sources import finmind_loader as fm
from data_sources import ingest
from data_sources import rate_limiter as rl
from data_sources.twse_loader import TWSENoDataError


class _FakeRedis:
    """token bucket 用到的最小子集：hash + WATCH/MULTI pipeline + 計數。"""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def incrby(self, key, n):
        self.counts[key] = self.counts.get(key, 0) + n
        return self.counts[key]

    def decrby(self, key, n):
        self.counts[key] = self.counts.get(key, 0) - n
        return self.counts[key]

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def pipeline(self):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def watch(self, key):
        pass

    def unwatch(self):
        pass

    def multi(self):
        self.ops = []

    def hset(self, key, mapping):
        self.ops.append(("hset", key, {k: str(v) for k, v in mapping.items()}))

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))

    def execute(self):
        for op, key, arg in self.ops:
            if op == "hset":
                self.r.hashes[key] = arg
            else:
                self.r.ttls[key] = arg


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t
        self.slept = 0.0

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.slept += s
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(rl.time, "time", c.time)
    monkeypatch.setattr(rl.time, "monotonic", c.monotonic)
    monkeypatch.setattr(rl.time, "sleep", c.sleep)
    monkeypatch.setattr(rl.random, "uniform", lambda a, b: 1.0)   # 去掉 jitter，等待時間可預測
    monkeypatch.setattr(rl.monitor, "mark", lambda *_: None)
    return c


Q = rl.Quota(rate_per_sec=1.0, burst=2, hourly_budget=0)


@pytest.fixture
def quota(monkeypatch):
    monkeypatch.setitem(rl.QUOTAS, "unit", Q)
    return Q


# ── T014 token 足夠即回、不足阻塞、逾時 RateLimitTimeout ──

def test_acquire_returns_immediately_while_tokens_last(clock, quota):
    r = _FakeRedis()
    assert rl.acquire("unit", client=r) == 0.0
    assert rl.acquire("unit", client=r) == 0.0                  # burst=2
    assert clock.slept == 0.0


def test_acquire_blocks_until_refilled(clock, quota):
    r = _FakeRedis()
    rl.acquire("unit", client=r)
    rl.acquire("unit", client=r)
    waited = rl.acquire("unit", client=r)                       # 桶空 → 等 1/rate 秒
    assert waited == pytest.approx(1.0) and clock.slept == pytest.approx(1.0)


def test_acquire_times_out(clock, quota):
    r = _FakeRedis()
    with pytest.raises(rl.RateLimitTimeout):
        rl.acquire("unit", cost=5, client=r, max_wait_sec=3)    # cost > burst，永遠拿不到


def test_unknown_provider_is_not_throttled(clock):
    assert rl.acquire("no-such-provider", client=_FakeRedis()) == 0.0


# ── T015 整點配額用罄 → RateLimitExhausted；超量的增量會退回 ──

def test_hourly_budget_exhausted_and_increment_rolled_back(clock, monkeypatch):
    monkeypatch.setitem(rl.QUOTAS, "unit", rl.Quota(rate_per_sec=100, burst=100, hourly_budget=3))
    r = _FakeRedis()
    for _ in range(3):
        rl.acquire("unit", client=r)
    for _ in range(5):                                          # 連續被擋也不會把計數灌大
        with pytest.raises(rl.RateLimitExhausted):
            rl.acquire("unit", client=r)
    assert r.counts[rl._hourly_key("unit")] == 3
    assert r.ttls[rl._hourly_key("unit")] == 7200


def test_hourly_budget_does_not_sleep_before_raising(clock, monkeypatch):
    monkeypatch.setitem(rl.QUOTAS, "unit", rl.Quota(rate_per_sec=0.01, burst=1, hourly_budget=1))
    r = _FakeRedis()
    rl.acquire("unit", client=r)
    with pytest.raises(rl.RateLimitExhausted):
        rl.acquire("unit", client=r)
    assert clock.slept == 0.0                                   # 用罄就直接拋，不白等 token


# ── T016 wall-clock 補 token；時鐘回退不扣 token ──

def test_tokens_refill_by_wall_clock(clock, quota):
    r = _FakeRedis()
    rl.acquire("unit", client=r)
    rl.acquire("unit", client=r)
    clock.t += 2.0                                              # 另一個 process 過了 2 秒
    assert rl._try_consume(r, "unit", 2, Q)                     # 補回 2 顆


def test_clock_going_backwards_never_removes_tokens(clock, quota):
    r = _FakeRedis()
    rl.acquire("unit", client=r)                                # 剩 1 顆
    clock.t -= 3600                                             # NTP 校時往回撥
    assert rl._try_consume(r, "unit", 1, Q)                     # 那 1 顆還在
    assert float(r.hashes[rl._key("unit")]["tokens"]) == 0.0


# ── T020 redis 故障 → 整點配額放行 ──

def test_hourly_budget_fails_open_when_redis_is_down():
    class Down:
        def incrby(self, *_):
            raise redis.ConnectionError("down")

    rl._check_hourly_budget(Down(), "unit", rl.Quota(1, 1, hourly_budget=1), 1)   # 不拋


# ── T017 FinMind 402 → Quota、403 ban → IPBanned；本機額度先擋 ──

class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def finmind(monkeypatch):
    sent = []
    state = {"resp": _Resp(200, {"status": 200, "data": [{"x": 1}]})}

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            sent.append(params)
            return state["resp"]

    monkeypatch.setattr(fm.httpx, "Client", Client)
    monkeypatch.setattr(fm.rate_limiter, "acquire", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(fm._request.retry, "sleep", lambda *_: None)   # 重試不真的睡
    return SimpleNamespace(sent=sent, state=state)


def test_finmind_402_is_quota_and_not_retried(finmind):
    finmind.state["resp"] = _Resp(402, {"status": 402}, "Requests reach the upper limit.")
    with pytest.raises(fm.FinMindQuotaExceeded):
        fm._request("TaiwanStockPrice")
    assert len(finmind.sent) == 1


def test_finmind_402_wrapped_in_200_is_quota(finmind):
    finmind.state["resp"] = _Resp(200, {"status": 402, "msg": "upper limit"})
    with pytest.raises(fm.FinMindQuotaExceeded):
        fm._request("TaiwanStockPrice")


def test_finmind_403_ban_is_ip_banned_with_retry_after(finmind):
    finmind.state["resp"] = _Resp(403, {"msg": "ip banned", "status": 403, "retry_after": 1653})
    with pytest.raises(fm.FinMindIPBanned) as exc:
        fm._request("TaiwanStockPrice")
    assert exc.value.retry_after == 1653 and len(finmind.sent) == 1


def test_finmind_transient_error_is_retried(finmind):
    finmind.state["resp"] = _Resp(500, None, "oops")
    with pytest.raises(fm.FinMindError):
        fm._request("TaiwanStockPrice")
    assert len(finmind.sent) == 3


def test_local_hourly_budget_blocks_before_any_http_call(finmind, monkeypatch):
    def exhausted(*_a, **_k):
        raise rl.RateLimitExhausted("finmind", 600)

    monkeypatch.setattr(fm.rate_limiter, "acquire", exhausted)
    with pytest.raises(fm.FinMindQuotaExceeded):
        fm._request("TaiwanStockPrice")
    assert finmind.sent == []


def test_backoff_errors_are_never_retried():
    assert not fm._should_retry(fm.FinMindQuotaExceeded())
    assert not fm._should_retry(fm.FinMindIPBanned(10))
    assert fm._should_retry(httpx.ConnectError("x"))


# ── T018 TPEx 失敗 → TWSE 仍落地、tpex 帶 error ──

class _Row:
    def __init__(self, ok=True):
        self.ok = ok

    def is_valid(self):
        return (self.ok, "" if self.ok else "bad")


def test_tpex_failure_does_not_block_twse(monkeypatch):
    written = []
    monkeypatch.setattr(ingest.twse_loader, "fetch_twse_daily", lambda d: (d, [_Row(), _Row()]))

    def tpex_down(d):
        raise httpx.ConnectError("tpex down")

    monkeypatch.setattr(ingest.twse_loader, "fetch_tpex_daily", tpex_down)
    monkeypatch.setattr(ingest.local_store, "write_prices",
                        lambda rows, market: written.append(len(rows)) or {"written": len(rows)})
    out = ingest.ingest_tw_prices(date(2026, 10, 14))
    assert out["sources"]["twse"]["valid"] == 2 and written == [2]
    assert "tpex down" in out["sources"]["tpex"]["error"]


# ── T019 假日往前回退；DQ 濾掉不合格列 ──

def test_fetch_with_fallback_walks_back_over_holidays():
    tried = []

    def fetch(d):
        tried.append(d)
        if d.weekday() >= 5:
            raise TWSENoDataError("holiday")
        return d, ["row"]

    actual, rows = ingest._fetch_with_fallback(fetch, date(2026, 10, 18))   # 週日
    assert actual == date(2026, 10, 16) and rows == ["row"]
    assert tried == [date(2026, 10, 18), date(2026, 10, 17), date(2026, 10, 16)]


def test_fetch_with_fallback_gives_up_after_max_back():
    def never(d):
        raise TWSENoDataError("none")

    with pytest.raises(TWSENoDataError):
        ingest._fetch_with_fallback(never, date(2026, 10, 14), max_back=3)


def test_dq_filter_drops_invalid_rows():
    good, bad = _Row(True), _Row(False)
    assert ingest._dq_filter([good, bad, good]) == [good, good]
