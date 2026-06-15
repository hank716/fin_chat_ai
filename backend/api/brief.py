"""晨報端點（M1 step 5；M1 驗收：POST /brief/morning → 瀏覽器看得到完整頁面）。"""
from __future__ import annotations

import secrets
import threading

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ai.gemini_client import GeminiError, GeminiQuotaExceeded
from reports import morning_brief
from reports.web_renderer import render_history_html, render_report_html

router = APIRouter(tags=["brief"])

# 全市場財報慢爬的單例守門：同時只跑一個（可重入靠磁碟快取續跑）
_crawl_state: dict[str, bool] = {"running": False}


@router.get("/", response_class=HTMLResponse)
async def home() -> str:
    """首頁＝歷史報告列表（登入後落地頁）＋本月全站 AI 花費 + 待機時段建議（皆即時）。"""
    from activity import monitor
    from config import settings
    from cost import tracker

    cost = {
        "month": tracker.current_month(),
        "month_total_twd": tracker.month_total(),
        "day_total_twd": tracker.today_total(),
        "monthly_limit_twd": float(settings.monthly_cost_limit_twd),
        "daily_limit_twd": float(settings.daily_cost_limit_twd),
    }
    activity = monitor.idle_report()
    from reports import strategy_calibration
    calibration = strategy_calibration.latest_summary()
    evaluation = strategy_calibration.latest_evaluation()
    return render_history_html(
        morning_brief.list_reports(), cost=cost, activity=activity,
        calibration=calibration, evaluation=evaluation,
    )


@router.get("/activity")
async def activity_report(days: int = Query(default=14, ge=1, le=35)) -> dict:
    """待機時段建議的原始資料（每小時活動熱度 + 建議待機窗 + 已累積天數）。"""
    from activity import monitor

    return monitor.idle_report(days=days)


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


@router.post("/brief/backtest")
async def post_backtest() -> dict:
    """手動觸發回測迴圈（純本地、零 LLM 花費）：回測已到期晨報 → 重建校準 → 訓練 edge 模型。

    平日由每次產晨報自動帶跑；此端點供隨時重算（如剛補了行情或想看最新校準）。
    """
    from starlette.concurrency import run_in_threadpool

    from reports import backtest, strategy_calibration

    def _run() -> dict:
        evald = backtest.run_due_evaluations()
        edge = strategy_calibration.train_edge_model()   # 先訓練→寫 edge_meta
        summary = strategy_calibration.rebuild()          # 再彙整，帶到最新 edge 狀態
        evaluation = strategy_calibration.evaluate_effectiveness()
        return {
            "evaluated": evald,
            "sample_n": summary.get("sample_n"),
            "metrics": summary.get("metrics"),
            "signal_ranking": summary.get("signal_ranking"),
            "calibration_text": summary.get("calibration_text"),
            "edge_model": edge,
            "evaluation": evaluation,
        }

    return await run_in_threadpool(_run)


@router.post("/brief/eval")
async def post_eval() -> dict:
    """成效量測（純本地）：把「策略準不準」變成數字——選股是否贏大盤、edge OOS、樣本是否足夠。"""
    from starlette.concurrency import run_in_threadpool

    from reports import strategy_calibration

    return await run_in_threadpool(strategy_calibration.evaluate_effectiveness)


@router.post("/brief/build-training-set")
async def post_build_training_set() -> dict:
    """重建歷史『回放選股規則』訓練集（純本地、CPU 數十秒）後重訓 edge 模型。

    讓 edge 模型一次取得數千筆與線上同分布的樣本（5/20 日各一份），不必等數週累積。
    """
    from starlette.concurrency import run_in_threadpool

    from reports import strategy_calibration, training_set

    def _run() -> dict:
        stats = training_set.build_training_set()
        edge = strategy_calibration.train_edge_model()
        strategy_calibration.rebuild()                 # 讓 calibration.json 帶到最新 edge 狀態（供首頁）
        strategy_calibration.evaluate_effectiveness()
        return {"training_set": stats, "edge_model": edge}

    return await run_in_threadpool(_run)


def _require_admin(x_admin_token: str | None) -> None:
    """管理端點權杖檢查。未設定 ADMIN_TOKEN → fail-closed（停用端點）；token 不符 → 401。"""
    from config import settings

    expected = settings.admin_token.strip()
    if not expected:
        raise HTTPException(status_code=503, detail="管理端點未啟用：請先在 .env 設定 ADMIN_TOKEN")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="X-Admin-Token 缺少或不正確")


@router.post("/admin/cost/calibrate")
async def calibrate_cost(
    month_total_twd: float = Query(..., description="Google 後台當月實際用量(TWD)，用它重設月度基準"),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    """把當月全站累計校準成後台實際金額（估算與後台必有落差時對齊；之後照常累加）。

    需帶 `X-Admin-Token` 標頭，值＝.env 的 ADMIN_TOKEN。
    """
    _require_admin(x_admin_token)
    from cost import tracker

    before = tracker.month_total()
    after = tracker.set_month_total(month_total_twd)
    return {
        "month": tracker.current_month(),
        "before_twd": before,
        "after_twd": after,
        "day_total_twd": tracker.today_total(),
    }


@router.post("/admin/data/purge-future")
async def purge_future_rows(
    market: str | None = Query(default=None, description="限定市場(tw/us/crypto)，留空=全部"),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    """清掉磁碟上既有的「未來日」幽靈列（價/籌碼/融券）。

    歷史 glitch（TWSE/TPEx 偶發回近未來日，舊 _assert_reasonable_date 用 abs()+30天容忍漏網）
    會以唯一 trade_date 落地、upsert keep-last 永不覆蓋，長期汙染 as_of=max(trade_date)，
    並讓新聞抓取對未來日空打、個股指標錨在偽資料上。冪等、可重複執行。

    需帶 `X-Admin-Token` 標頭，值＝.env 的 ADMIN_TOKEN。
    """
    _require_admin(x_admin_token)
    from storage import local_store

    return local_store.purge_future_rows(market)


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
