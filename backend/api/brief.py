"""晨報端點（M1 step 5；M1 驗收：POST /brief/morning → 瀏覽器看得到完整頁面）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from reports import morning_brief
from reports.web_renderer import render_report_html

router = APIRouter(tags=["brief"])


@router.post("/brief/morning")
async def post_morning(raw_query: str | None = Query(default=None)) -> dict:
    report = morning_brief.generate_morning_brief(raw_query=raw_query)
    return {
        "report_id": report["report_id"],
        "data_as_of": report["data_as_of"],
        "landed_symbols": report["landed_symbols"],
        "url": f"/report/{report['report_id']}",
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
