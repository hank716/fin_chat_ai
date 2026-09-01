"""晨報單例鎖（api/brief.py `_brief_state`）的迴歸測試。

為什麼值得一支專門的測試：整條晨報管線 20~35 分鐘，而 scheduler 的 catch-up 只看
「今日是否已有報告」——若它在晨報跑到一半時重啟，就會再打一次 `POST /brief/morning`，
同一天兩份 LLM 帳單。2026-08-30 加鎖時只做過一次性 stub 實測，沒留下可重跑的護欄。

比重複計費更糟的是鎖沒被釋放：那會讓晨報**永久**卡住，所以例外路徑也一併測。
測試不碰真實管線——generate_morning_brief 整支被換成受 Event 控制的假貨。
"""
from __future__ import annotations

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai.errors import LLMQuotaExceeded
from api import brief
from reports import morning_brief


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(brief.router)
    brief._brief_state["running"] = False        # 與其他測試隔離：進場先確保鎖是開的
    with TestClient(app) as c:
        yield c
    brief._brief_state["running"] = False


def _fake_report(rid: str = "morning_test") -> dict:
    return {"report_id": rid, "data_as_of": "2026-09-01", "landed_symbols": []}


def test_second_request_is_rejected_while_running(client, monkeypatch):
    """A 還在跑時打 B → B 立刻被擋，且真正的產生函式只被呼叫一次。"""
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def fake_generate(*, raw_query=None, push_discord=True, publish=True):
        calls.append("run")
        entered.set()
        assert release.wait(timeout=10), "測試自身逾時：release 沒被設起來"
        return _fake_report()

    monkeypatch.setattr(morning_brief, "generate_morning_brief", fake_generate)

    a_result: dict = {}

    def run_a():
        a_result["resp"] = client.post("/brief/morning")

    t = threading.Thread(target=run_a, daemon=True)
    t.start()
    assert entered.wait(timeout=10), "第一次請求沒進到產生函式"

    # A 卡在管線裡時打第二次：不排隊、不重跑，直接回「已經在跑了」
    b = client.post("/brief/morning")
    assert b.status_code == 200
    assert b.json() == {"started": False, "reason": "morning brief already running"}

    release.set()
    t.join(timeout=15)
    assert a_result["resp"].status_code == 200
    assert a_result["resp"].json()["report_id"] == "morning_test"
    assert calls == ["run"], "第二次請求不該真的跑起來（那就是雙倍 LLM 帳單）"


def test_lock_released_after_success(client, monkeypatch):
    """A 跑完後鎖要放掉——否則第二天的排程會被昨天的鎖擋住。"""
    monkeypatch.setattr(morning_brief, "generate_morning_brief",
                        lambda **kw: _fake_report("morning_1"))
    assert client.post("/brief/morning").json()["report_id"] == "morning_1"
    assert brief._brief_state["running"] is False

    monkeypatch.setattr(morning_brief, "generate_morning_brief",
                        lambda **kw: _fake_report("morning_2"))
    assert client.post("/brief/morning").json()["report_id"] == "morning_2"


def test_lock_released_when_pipeline_raises(client, monkeypatch):
    """例外路徑也要釋放：一次配額用盡若把鎖留在 True，晨報就永久鎖死了。"""
    def boom(**kw):
        raise LLMQuotaExceeded("quota exhausted")

    monkeypatch.setattr(morning_brief, "generate_morning_brief", boom)
    assert client.post("/brief/morning").status_code == 503
    assert brief._brief_state["running"] is False, "503 之後鎖沒放掉＝晨報永久卡死"

    # 鎖確實放掉了：下一次請求能正常進場
    monkeypatch.setattr(morning_brief, "generate_morning_brief",
                        lambda **kw: _fake_report("morning_after_503"))
    assert client.post("/brief/morning").json()["report_id"] == "morning_after_503"
