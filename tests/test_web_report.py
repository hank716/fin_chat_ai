"""spec 007 測試基線：單篇報告 HTML/JSON/MD 端點、404、autoescape、首頁缺資料不報錯。

透過 FastAPI TestClient 打真正的路由與 Jinja 模板；報告檔放 tmp_path。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai.schemas import BriefResult, BriefSection, Evidence, WatchItem
from api import brief as brief_api
from reports import morning_brief
from reports.json_builder import build_report_dict
from reports.markdown_builder import build_markdown

RID = "morning_20261014_082000"
XSS = '<script>alert("x")</script>'


def _report():
    result = BriefResult(
        headline=f"今日偏多 {XSS}", data_as_of=date(2026, 10, 14),
        sections=[BriefSection(title="台股", narrative="權值股走強", evidence=[
            Evidence(label="台積電收盤", value="900", source_ref='tw.stocks["2330"].close')])],
        tw_watchlist=[WatchItem(symbol="2330", name="台積電", thesis=f"技術面偏多 {XSS}",
                                signals=["外資連買3日"], target_price="950", stop_loss="860")],
    )
    gen = datetime(2026, 10, 14, 8, 20, tzinfo=ZoneInfo("Asia/Taipei"))
    report = build_report_dict(result, generated_at=gen, raw_query=None)
    report.update({"report_id": RID, "report_date": "2026-10-14",
                   "markdown": build_markdown(result, generated_at=gen, raw_query=None),
                   "guardrail": {"passed": True, "error_count": 0, "warning_count": 0, "violations": []},
                   "cost": {"brief_twd": 12.3}})
    return report


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(morning_brief, "REPORTS_DIR", tmp_path)
    report = _report()
    (tmp_path / f"{RID}.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (tmp_path / f"{RID}.md").write_text(report["markdown"], encoding="utf-8")
    app = FastAPI()
    app.include_router(brief_api.router)
    return TestClient(app)


# ── T008 GET /report/{id} → 200 HTML，含候選與 evidence ──

def test_report_page_renders_candidates_and_evidence(client):
    r = client.get(f"/report/{RID}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "台積電" in r.text and "2330" in r.text
    assert "台積電收盤" in r.text


# ── T009 .json / .md 後綴回對應格式 ──

def test_json_and_markdown_suffixes(client):
    j = client.get(f"/report/{RID}.json")
    assert j.status_code == 200 and j.headers["content-type"].startswith("application/json")
    assert j.json()["report_id"] == RID
    md = client.get(f"/report/{RID}.md")
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/plain")
    assert "台積電" in md.text


# ── T010 不存在或不合法 → 404 ──

@pytest.mark.parametrize("path", ["/report/morning_20200101_000000", "/report/morning_20200101_000000.json",
                                  "/report/morning_20200101_000000.md", "/report/..%2F..%2Fetc"])
def test_missing_or_illegal_report_is_404(client, monkeypatch, path):
    from publish import pcloud_backup
    monkeypatch.setattr(pcloud_backup, "restore_report", lambda rid: False)
    assert client.get(path).status_code == 404


# ── T011 autoescape：模型輸出裡的 HTML 會被跳脫 ──

def test_model_text_is_html_escaped(client):
    html = client.get(f"/report/{RID}").text
    assert XSS not in html
    assert "&lt;script&gt;" in html


# ── T012 首頁：各面板缺資料時略過、不報錯 ──

def test_home_renders_with_every_panel_empty(client, monkeypatch):
    from activity import monitor
    from cost import tracker
    from reports import strategy_calibration

    for name, val in (("current_month", "2026-10"), ("month_total", 0.0), ("today_total", 0.0),
                      ("month_by_provider", {})):
        monkeypatch.setattr(tracker, name, lambda v=val: v)
    monkeypatch.setattr(monitor, "idle_report", lambda: {})
    monkeypatch.setattr(strategy_calibration, "latest_summary", lambda: {})
    monkeypatch.setattr(strategy_calibration, "latest_evaluation", lambda: {})

    def broken_status():
        raise RuntimeError("history crawl state unreadable")

    from data_sources import history_crawl
    monkeypatch.setattr(history_crawl, "status", broken_status)
    r = client.get("/")
    assert r.status_code == 200 and RID in r.text


def test_history_template_tolerates_all_none():
    from reports.web_renderer import render_history_html
    html = render_history_html([], cost=None, activity=None, calibration=None, evaluation=None, history=None)
    assert "<html" in html.lower()
