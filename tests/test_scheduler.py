"""spec 010 測試基線：排程時間解析、交易日判斷、catch-up 補產、慢爬補觸發、例外不中止。

scheduler 是獨立服務（自己的 requirements），測試環境不裝 APScheduler——它只在 main() 用到，
這裡以空殼模組頂替後按檔案路徑載入。HTTP 與時鐘全換成替身。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

if "apscheduler" not in sys.modules:
    try:
        import apscheduler  # noqa: F401
    except ImportError:
        for name, attr in (("apscheduler.schedulers.blocking", "BlockingScheduler"),
                           ("apscheduler.triggers.cron", "CronTrigger")):
            parts = name.split(".")
            for i in range(1, len(parts) + 1):
                sys.modules.setdefault(".".join(parts[:i]), types.ModuleType(".".join(parts[:i])))
            setattr(sys.modules[name], attr, object)

_PATH = Path(__file__).resolve().parents[1] / "scheduler" / "scheduler.py"
_spec = importlib.util.spec_from_file_location("scheduler_under_test", _PATH)
sch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sch)


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body or {}, ""

    def json(self):
        return self._body


class _Http:
    """記錄每一次 get/post；回應依 URL 結尾查表，查不到就回 200 {}。"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.routes: dict[str, object] = {}

    def _answer(self, method, url):
        self.calls.append((method, url.removeprefix(sch.BACKEND_URL)))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _Resp()

    def get(self, url, **_k):
        return self._answer("GET", url)

    def post(self, url, **_k):
        return self._answer("POST", url)

    def posts(self):
        return [u for m, u in self.calls if m == "POST"]


@pytest.fixture
def http(monkeypatch):
    h = _Http()
    monkeypatch.setattr(sch.httpx, "get", h.get)
    monkeypatch.setattr(sch.httpx, "post", h.post)
    monkeypatch.setattr(sch, "_wait_backend_ready", lambda max_wait=120.0: True)
    return h


def _at(monkeypatch, hh, mm):
    """把 scheduler 看到的「現在」釘在今天 hh:mm（台北時間）。"""
    fixed = datetime(2026, 10, 15, hh, mm, tzinfo=sch.TZ)

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(sch, "datetime", _Fixed)


# ── T012 _parse_times ──

def test_parse_times_sorts_dedupes_and_ignores_garbage():
    assert sch._parse_times("14:00, 08:30,08:30,nope,25") == [(8, 30), (14, 0)]


def test_parse_times_empty_defaults_to_0830():
    assert sch._parse_times("") == [(8, 30)]
    assert sch._parse_times(" , ,bad") == [(8, 30)]


# ── T013 generate_brief：非交易日略過、交易日 POST ──

def test_scheduled_brief_skips_non_trading_day(http):
    http.routes["/brief/status"] = _Resp(body={"is_trading_day": False})
    sch.generate_brief()
    assert http.posts() == []


def test_scheduled_brief_posts_on_trading_day(http):
    http.routes["/brief/status"] = _Resp(body={"is_trading_day": True})
    http.routes["/brief/morning"] = _Resp(body={"report_id": "morning_x"})
    sch.generate_brief()
    assert http.posts() == ["/brief/morning"]


def test_catch_up_reason_does_not_recheck_trading_day(http):
    sch.generate_brief(reason="catch-up")                     # catch_up 已判過交易日
    assert http.calls == [("POST", "/brief/morning")]


# ── T014 catch_up：補產 / 已有報告 / 未到排程 / 非交易日 ──

@pytest.mark.parametrize("status,hhmm,expect_post", [
    ({"has_today": False, "is_trading_day": True}, (9, 0), True),     # 漏跑 → 補產
    ({"has_today": True, "is_trading_day": True}, (9, 0), False),     # 今天已有
    ({"has_today": False, "is_trading_day": True}, (7, 0), False),    # 還沒到 08:30
    ({"has_today": False, "is_trading_day": False}, (9, 0), False),   # 非交易日
])
def test_catch_up_branches(http, monkeypatch, status, hhmm, expect_post):
    monkeypatch.setattr(sch, "REPORT_TIMES", "08:30,14:00")
    _at(monkeypatch, *hhmm)
    http.routes["/brief/status"] = _Resp(body=status)
    sch.catch_up()
    assert http.posts() == (["/brief/morning"] if expect_post else [])


def test_catch_up_skips_when_backend_never_ready(http, monkeypatch):
    monkeypatch.setattr(sch, "_wait_backend_ready", lambda max_wait=120.0: False)
    sch.catch_up()
    assert http.calls == []


def test_catch_up_skips_when_status_query_fails(http, monkeypatch):
    _at(monkeypatch, 9, 0)
    http.routes["/brief/status"] = ConnectionError("down")
    sch.catch_up()                                            # 不知道今天有沒有報告就不補（免重複花錢）
    assert http.posts() == []


# ── T015 crawl_catch_up / history_catch_up ──

def test_crawl_catch_ups_do_nothing_when_unconfigured(http, monkeypatch):
    monkeypatch.setattr(sch, "CRAWL_TIMES", "")
    monkeypatch.setattr(sch, "HISTORY_CRAWL_TIMES", "  ")
    sch.crawl_catch_up()
    sch.history_catch_up()
    assert http.calls == []


def test_crawl_catch_up_triggers_after_earliest_time(http, monkeypatch):
    monkeypatch.setattr(sch, "CRAWL_TIMES", "21:00,09:00")
    _at(monkeypatch, 10, 0)
    sch.crawl_catch_up()
    assert http.posts() == ["/brief/prefetch"]


def test_crawl_catch_up_waits_before_earliest_time(http, monkeypatch):
    monkeypatch.setattr(sch, "CRAWL_TIMES", "09:00")
    _at(monkeypatch, 8, 0)
    sch.crawl_catch_up()
    assert http.posts() == []


def test_history_catch_up_triggers_after_time(http, monkeypatch):
    monkeypatch.setattr(sch, "HISTORY_CRAWL_TIMES", "01:00")
    _at(monkeypatch, 10, 0)
    sch.history_catch_up()
    assert http.posts() == ["/brief/backfill-history"]


# ── T016 查詢失敗時保守視為交易日 ──

def test_trading_day_query_failure_is_treated_as_trading(http):
    http.routes["/brief/status"] = ConnectionError("down")
    assert sch._is_trading_day() is True


def test_trading_day_missing_field_is_treated_as_trading(http):
    http.routes["/brief/status"] = _Resp(body={})
    assert sch._is_trading_day() is True


# ── T017 觸發函式遇 httpx 例外只記 error、不拋 ──

@pytest.mark.parametrize("job", [
    lambda: sch.generate_brief(reason="catch-up"),
    lambda: sch.prefetch_fundamentals(reason="catch-up"),
    lambda: sch.crawl_fundamentals(reason="catch-up"),
    lambda: sch.crawl_listed_history_job(reason="catch-up"),
    sch.crawl_tpex_prices_job,
    sch.crawl_fundamentals_history_job,
])
def test_jobs_swallow_http_errors(http, job, caplog):
    for suffix in ("/brief/morning", "/brief/prefetch", "/brief/backfill-history",
                   "/brief/backfill-tpex-prices", "/brief/backfill-fundamentals"):
        http.routes[suffix] = ConnectionError("backend down")
    job()                                                     # 不拋
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_scan_corrupt_swallows_http_errors(http, monkeypatch, caplog):
    monkeypatch.setattr(sch, "ADMIN_TOKEN", "t")
    http.routes["/admin/data/scan-corrupt"] = ConnectionError("down")
    sch.scan_corrupt()
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_scan_corrupt_skips_without_admin_token(http, monkeypatch):
    monkeypatch.setattr(sch, "ADMIN_TOKEN", "")
    sch.scan_corrupt()
    assert http.calls == []
