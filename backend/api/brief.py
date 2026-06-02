"""晨報端點（M1 step 5；M1 驗收：POST /brief/morning → 瀏覽器看得到完整頁面）。"""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ai.gemini_client import GeminiError, GeminiQuotaExceeded
from reports import morning_brief
from reports.web_renderer import render_history_html, render_report_html

router = APIRouter(tags=["brief"])

# 全市場財報慢爬的單例守門：同時只跑一個（可重入靠磁碟快取續跑）
_crawl_state: dict[str, bool] = {"running": False}


@router.get("/", response_class=HTMLResponse)
async def home() -> str:
    """首頁＝歷史報告列表（登入後落地頁）＋本月全站 AI 花費（即時、含晨報＋問答）。"""
    from config import settings
    from cost import tracker

    cost = {
        "month": tracker.current_month(),
        "month_total_twd": tracker.month_total(),
        "day_total_twd": tracker.today_total(),
        "monthly_limit_twd": float(settings.monthly_cost_limit_twd),
        "daily_limit_twd": float(settings.daily_cost_limit_twd),
    }
    return render_history_html(morning_brief.list_reports(), cost=cost)


@router.post("/brief/morning")
async def post_morning(
    raw_query: str | None = Query(default=None),
    push_discord: bool = Query(default=True),
    publish: bool = Query(default=True),
) -> dict:
    try:
        report = morning_brief.generate_morning_brief(
            raw_query=raw_query, push_discord=push_discord, publish=publish
        )
    except GeminiQuotaExceeded as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Gemini 配額已用盡（換個有額度的 GEMINI_MODEL 或等每日重置）：{exc}",
        ) from exc
    except GeminiError as exc:
        raise HTTPException(status_code=503, detail=f"Gemini 暫時無法使用：{exc}") from exc
    return {
        "report_id": report["report_id"],
        "data_as_of": report["data_as_of"],
        "landed_symbols": report["landed_symbols"],
        "url": f"/report/{report['report_id']}",
    }


@router.post("/brief/prefetch")
async def post_prefetch(
    scope: str = Query(default="focus"),       # focus=當天焦點股(快) / full=焦點優先再慢慢掃全市場
    force: bool = Query(default=False),
    max_seconds: float | None = Query(default=None),
) -> dict:
    """（可選）預抓基本面到磁碟快取，讓之後的晨報少打 FinMind。

    scope=focus：循序跑、被 finmind limiter 節流（數分鐘），丟 threadpool 等它完成回結果。
    scope=full ：全市場慢爬可數小時～23h，改「背景 detached 執行」立刻回，避免 HTTP timeout；
                 同一時間只允許一個爬蟲（可重入：靠磁碟快取續跑）。
    """
    from starlette.concurrency import run_in_threadpool

    from processor.prefetch_fundamentals import prefetch

    if scope == "full":
        if _crawl_state["running"]:
            return {"started": False, "reason": "full crawl already running"}

        def _run() -> None:
            _crawl_state["running"] = True
            try:
                prefetch(scope="full", force=force, max_seconds=max_seconds)
            finally:
                _crawl_state["running"] = False

        threading.Thread(target=_run, name="fundamentals-crawl", daemon=True).start()
        return {"started": True, "scope": "full", "background": True}

    return await run_in_threadpool(prefetch, scope=scope, force=force, max_seconds=max_seconds)


@router.get("/brief/status")
async def status() -> dict:
    """排程 catch-up 用：今日（schedule_tz）是否已產生報告。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from config import settings

    from trading_calendar import is_tw_trading_day

    today = datetime.now(ZoneInfo(settings.schedule_tz)).date()
    return {
        "schedule_date": today.isoformat(),
        "is_trading_day": is_tw_trading_day(today),
        "has_today": morning_brief.report_date_exists(today),
        "latest_report_id": morning_brief.latest_report_id(),
    }


@router.get("/brief/latest")
async def latest() -> RedirectResponse:
    rid = morning_brief.latest_report_id()
    if not rid:
        raise HTTPException(status_code=404, detail="尚無報告，請先 POST /brief/morning")
    return RedirectResponse(url=f"/report/{rid}")


# 具體後綴路由要排在泛用 /report/{id} 之前
@router.get("/report/{report_id}.json")
async def get_report_json(report_id: str) -> JSONResponse:
    report = morning_brief.load_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    return JSONResponse(report)


@router.get("/report/{report_id}.md", response_class=PlainTextResponse)
async def get_report_md(report_id: str) -> str:
    md = morning_brief.load_markdown(report_id)
    if md is None:
        raise HTTPException(status_code=404, detail="report not found")
    return md


@router.get("/report/{report_id}", response_class=HTMLResponse)
async def get_report(report_id: str) -> str:
    report = morning_brief.load_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    return render_report_html(report)
