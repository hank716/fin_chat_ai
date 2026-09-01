"""晨報端點（M1 step 5；M1 驗收：POST /brief/morning → 瀏覽器看得到完整頁面）。"""
from __future__ import annotations

import logging
import secrets
import threading

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ai.errors import LLMError, LLMQuotaExceeded
from reports import morning_brief
from reports.web_renderer import render_history_html, render_report_html

router = APIRouter(tags=["brief"])

logger = logging.getLogger("ai-market-backend.api.brief")

# 晨報產生的單例守門：整條管線 20~35 分鐘，而 scheduler 的 catch-up 只看「今日是否已有報告」，
# 若它在晨報跑到一半時重啟就會再打一次 → 同一天兩份 LLM 帳單。這道鎖是省錢的保險。
_brief_state: dict[str, bool] = {"running": False}
# 全市場財報慢爬的單例守門：同時只跑一個（可重入靠磁碟快取續跑）
_crawl_state: dict[str, bool] = {"running": False}
# 歷史行情慢爬的單例守門（軌道 A 上市 / 軌道 B 上櫃價各一）
_history_state: dict[str, bool] = {"listed": False, "tpex": False, "fund": False}


def _render_home() -> str:
    """首頁內容組裝（同步；全是阻塞的 redis / 檔案讀取，故由 handler 丟 threadpool 跑）。"""
    from activity import monitor
    from config import settings
    from cost import tracker

    cost = {
        "month": tracker.current_month(),
        "month_total_twd": tracker.month_total(),
        "day_total_twd": tracker.today_total(),
        "monthly_limit_twd": float(settings.monthly_cost_limit_twd),
        "daily_limit_twd": float(settings.daily_cost_limit_twd),
        # provider 拆分（spec 022）：合計看不出 Gemini 與 Claude 各佔多少，
        # 而那正是換供應商後最需要盯的數字。
        "by_provider": tracker.month_by_provider(),
    }
    activity = monitor.idle_report()
    from reports import strategy_calibration
    calibration = strategy_calibration.latest_summary()
    evaluation = strategy_calibration.latest_evaluation()
    try:
        from data_sources.history_crawl import status as history_status
        history = history_status()
    except Exception:  # noqa: BLE001 — 慢爬狀態讀取失敗不影響首頁
        history = None
    return render_history_html(
        morning_brief.list_reports(), cost=cost, activity=activity,
        calibration=calibration, evaluation=evaluation, history=history,
    )


@router.get("/", response_class=HTMLResponse)
async def home() -> str:
    """首頁＝歷史報告列表（登入後落地頁）＋本月全站 AI 花費 + 待機時段建議（皆即時）。"""
    return await run_in_threadpool(_render_home)


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
    if _brief_state["running"]:
        # 不排隊、不重跑：直接告訴呼叫端「已經在跑了」。scheduler 逾時後的補打、
        # 或人工重按，都不該再燒一份 LLM 費用。
        logger.warning("晨報已在產生中，忽略這次重複請求")
        return {"started": False, "reason": "morning brief already running"}
    _brief_state["running"] = True
    try:
        # ⚠️ **必須**走 threadpool：generate_morning_brief 是同步阻塞呼叫，且整條管線
        # （資料抓取 ~15 分 + LLM ~6 分 + 回測迴圈 ~12 分）動輒 30 分鐘以上。直接在
        # `async def` handler 裡呼叫會把 uvicorn 的 event loop 卡死，晨報跑完前**整站**
        # 都無法服務——/health、首頁、報告頁全部 timeout（2026-08-06 實測複現）。
        # 本檔其他重活（prefetch / backtest / eval / training_set）早就是這個寫法。
        report = await run_in_threadpool(
            morning_brief.generate_morning_brief,
            raw_query=raw_query, push_discord=push_discord, publish=publish,
        )
    except LLMQuotaExceeded as exc:
        raise HTTPException(
            status_code=503,
            detail=f"LLM 配額已用盡（換個有額度的模型或等每日重置）：{exc}",
        ) from exc
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=f"LLM 暫時無法使用：{exc}") from exc
    finally:
        _brief_state["running"] = False
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
            except Exception:  # noqa: BLE001 — 背景執行緒的例外要落成 log，不能只留裸 traceback
                logger.exception("全市場財報慢爬中止")
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
        risk = strategy_calibration.train_risk_model()   # 回撤風險模型（與方向並存）→寫 risk_meta
        rank = strategy_calibration.train_rank_model()   # 報酬 rank 模型（殘差方向，rank-IC）→寫 rank_meta
        meta = strategy_calibration.train_meta_model()   # meta-labeling（該不該下手，triple-barrier）→寫 meta_meta
        summary = strategy_calibration.rebuild()          # 再彙整，帶到最新 edge 狀態
        evaluation = strategy_calibration.evaluate_effectiveness()
        return {
            "evaluated": evald,
            "sample_n": summary.get("sample_n"),
            "metrics": summary.get("metrics"),
            "signal_ranking": summary.get("signal_ranking"),
            "calibration_text": summary.get("calibration_text"),
            "edge_model": edge,
            "risk_model": risk,
            "rank_model": rank,
            "meta_model": meta,
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
        risk = strategy_calibration.train_risk_model()
        rank = strategy_calibration.train_rank_model()
        meta = strategy_calibration.train_meta_model()
        regime = strategy_calibration.backtest_market_regime()   # 市場恐慌 regime gate
        strategy_calibration.rebuild()                 # 讓 calibration.json 帶到最新 edge 狀態（供首頁）
        strategy_calibration.evaluate_effectiveness()
        return {"training_set": stats, "edge_model": edge, "risk_model": risk,
                "rank_model": rank, "meta_model": meta, "market_regime": regime}

    return await run_in_threadpool(_run)


@router.post("/brief/battlefield-experiment")
async def post_battlefield_experiment(smallcap_min: float = Query(default=5_000_000)) -> dict:
    """改戰場另池實驗（純本地、零 LLM）：比較主池/中小型股/事件窗的 edge/risk/rank OOS 表現。

    只回報指標、不存模型、不動晨報；用來判斷小型股或 PEAD 事件窗是否有更強的方向訊號。
    """
    from starlette.concurrency import run_in_threadpool

    from reports import strategy_calibration

    return await run_in_threadpool(strategy_calibration.run_battlefield_experiment, smallcap_min)


@router.post("/brief/smallcap-sleeve-backtest")
async def post_smallcap_sleeve_backtest(top_k: int = Query(default=3),
                                        smallcap_min: float = Query(default=5_000_000)) -> dict:
    """小型股 5 日 rank sleeve 的扣成本淨報酬回測（純本地、零 LLM）。結果存
    storage/strategy/smallcap_sleeve_backtest.json。"""
    from starlette.concurrency import run_in_threadpool

    from reports import strategy_calibration

    return await run_in_threadpool(
        strategy_calibration.backtest_smallcap_sleeve, smallcap_min=smallcap_min, top_k=top_k)


@router.post("/brief/sizing-backtest")
async def post_sizing_backtest(horizon: int | None = Query(default=None)) -> dict:
    """部位 sizing 的淨 P&L 回測（純本地、零 LLM）：比較 risk×meta 加權 vs 等權，扣成本。
    結果存 storage/strategy/sizing_backtest.json，best_scheme 決定 serving 是否啟用 sizing。"""
    from starlette.concurrency import run_in_threadpool

    from reports import strategy_calibration

    return await run_in_threadpool(strategy_calibration.backtest_sizing, horizon=horizon)


@router.post("/brief/backfill-history")
async def post_backfill_history(max_minutes: float | None = Query(default=None)) -> dict:
    """軌道 A：上市歷史慢爬（TWSE 單日端點，不打 FinMind）。背景執行、單例、立即回。"""
    if _history_state["listed"]:
        return {"started": False, "reason": "listed history crawl already running"}

    def _run() -> None:
        _history_state["listed"] = True
        try:
            from data_sources.history_crawl import crawl_listed_history
            crawl_listed_history(max_seconds=(max_minutes * 60) if max_minutes else None)
        finally:
            _history_state["listed"] = False

    threading.Thread(target=_run, name="history-listed", daemon=True).start()
    return {"started": True, "track": "listed", "background": True}


@router.post("/brief/backfill-tpex-prices")
async def post_backfill_tpex_prices(max_calls: int | None = Query(default=None)) -> dict:
    """軌道 B：上櫃個股價慢爬（FinMind per-stock，每小時小批、嚴守額度）。背景執行、單例、立即回。"""
    if _history_state["tpex"]:
        return {"started": False, "reason": "tpex price crawl already running"}

    def _run() -> None:
        _history_state["tpex"] = True
        try:
            from data_sources.history_crawl import crawl_tpex_prices
            crawl_tpex_prices(max_calls=max_calls)
        finally:
            _history_state["tpex"] = False

    threading.Thread(target=_run, name="history-tpex", daemon=True).start()
    return {"started": True, "track": "tpex_prices", "background": True}


@router.post("/brief/backfill-fundamentals")
async def post_backfill_fundamentals(max_calls: int | None = Query(default=None)) -> dict:
    """軌道 C：基本面歷史慢爬（FinMind 季/月報 point-in-time，每小時小批、嚴守額度）。背景、單例、立即回。"""
    if _history_state["fund"]:
        return {"started": False, "reason": "fundamentals crawl already running"}

    def _run() -> None:
        _history_state["fund"] = True
        try:
            from data_sources.history_crawl import crawl_fundamentals_history
            crawl_fundamentals_history(max_calls=max_calls)
        finally:
            _history_state["fund"] = False

    threading.Thread(target=_run, name="history-fund", daemon=True).start()
    return {"started": True, "track": "fundamentals", "background": True}


@router.post("/brief/backfill-taifex")
async def post_backfill_taifex(years: int = Query(default=2)) -> dict:
    """回填台期交所選擇權 Put/Call Ratio（~years 年，市場恐慌 gauge 來源）。純本地、零 LLM。"""
    from starlette.concurrency import run_in_threadpool

    from data_sources import taifex_loader

    return await run_in_threadpool(taifex_loader.backfill, years)


@router.get("/brief/history-status")
async def get_history_status() -> dict:
    """歷史慢爬進度（目標/已回溯最早日/上櫃完成數）。"""
    from data_sources.history_crawl import status
    return status()


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


@router.post("/admin/data/scan-corrupt")
async def scan_corrupt_parquet(
    quarantine: bool = Query(default=False, description="True=把壞檔改名隔離（下次寫入自動重建）"),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    """掃全庫 parquet，找出打不開的死檔（可選隔離）。冪等、可重複執行。

    2026-08-30 事故：容器 rebuild 把一次 `to_parquet` 砍在半路，`tw/_margin/5530.parquet`
    被截斷成沒有結尾 magic bytes 的死檔，之後讀它就 ArrowInvalid，往上炸掉整個晨報特徵層。
    事後盤點壞的是**兩**檔（5530 融資券、5202 籌碼），且 5202 是這支掃過之後才由讀取端的
    read_parquet_safe 抓到的——所以這支是照明燈而非保證，自癒仍歸 read_parquet_safe。
    寫入端已改原子寫入不再產生半截檔，這支是「萬一還是有」時的照明燈與清道夫。

    需帶 `X-Admin-Token` 標頭，值＝.env 的 ADMIN_TOKEN。
    """
    _require_admin(x_admin_token)
    from storage import local_store

    return await run_in_threadpool(local_store.scan_corrupt_parquet, quarantine)


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
