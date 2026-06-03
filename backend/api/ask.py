"""即時查詢端點（M4）：Discord bot 把使用者問題轉來，grounded 在最新晨報作答。

流程：預算檢查 → 取最新報告 → grounded Gemini Q&A → 估算並記錄成本 → 回 answer+成本。
資料計算不用 Gemini（§25.1）；只有作答用。bot 只是薄轉接，Gemini/成本邏輯都在這。
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai import gemini_client
from ai.gemini_client import GeminiError, GeminiQuotaExceeded
from ai.prompts import build_qa_prompt
from cost import tracker
from processor import tw_features
from reports import morning_brief

# 台股代號：4–6 位數字 + 可選 1 個字母後綴（如 2330 / 0050 / 00685L / 00919）
_TW_CODE = re.compile(r"(?<!\d)(\d{4,6}[A-Z]?)(?!\d)")


def _ondemand_symbols(question: str, known: set[str], limit: int = 3) -> dict:
    """抓問題中清單外的台股代號 → 即時查其資料（查無/失敗則略過）。"""
    out: dict = {}
    for sym in _TW_CODE.findall(question):
        if sym in known or sym in out:
            continue
        try:
            blk = tw_features.build_adhoc_symbol(sym)
        except Exception as exc:  # noqa: BLE001
            logger.warning("即時查 %s 失敗: %s", sym, exc)
            blk = None
        if blk:
            out[sym] = blk
        if len(out) >= limit:
            break
    return out

logger = logging.getLogger("ai-market-backend.ask")

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    user_id: str
    question: str


class AskResponse(BaseModel):
    answer: str
    report_id: str
    cost_twd: float
    today_spent: float
    daily_limit: float
    budget_exceeded: bool = False


@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    ok, reason, spent, limit = tracker.check_budget()
    if not ok:
        return AskResponse(
            answer=f"{reason}。請明天再試或調整上限。",
            report_id="", cost_twd=0.0, today_spent=spent, daily_limit=limit,
            budget_exceeded=True,
        )

    rid = morning_brief.latest_report_id()
    report = morning_brief.load_report(rid) if rid else None
    if report is None:
        raise HTTPException(status_code=404, detail="尚無晨報可查詢，請先產生晨報")

    known = set((report.get("features", {}).get("tw", {}) or {}).get("stocks", {}).keys())
    on_demand = _ondemand_symbols(req.question, known)
    # 基本面（月營收 YoY/MoM）：對問題中的台股代號 on-demand 抓
    from processor.fundamentals import build_fundamentals
    fundamentals: dict = {}
    for sym in _TW_CODE.findall(req.question)[:3]:
        fu = build_fundamentals(sym)
        if fu:
            fundamentals[sym] = fu
    prompt = build_qa_prompt(req.question, report, on_demand=on_demand, fundamentals=fundamentals)
    try:
        answer, usage = gemini_client.generate_text(prompt, use_search=True)
    except GeminiQuotaExceeded as exc:
        raise HTTPException(status_code=503, detail=f"Gemini 配額用盡：{exc}") from exc
    except GeminiError as exc:
        raise HTTPException(status_code=503, detail=f"Gemini 暫時無法使用：{exc}") from exc

    from config import settings
    # grounded=True：問答用 Google 搜尋，含 cache 折扣與 grounding 邊際費用
    cost = tracker.cost_of_usage(usage, settings.gemini_model_qa, grounded=True)
    tracker.record_cost(cost)
    day_total = tracker.today_total()
    logger.info("ask user=%s tokens=%s cost=NT$%.4f 今日全站=NT$%.2f",
                req.user_id, usage, cost, day_total)
    answer = (answer or "（無回應）") + \
        "\n\n⚠️ 以上方向/目標價/止損為技術面輔助參考，非保證、請自行判斷風險。"
    return AskResponse(
        answer=answer,
        report_id=rid,
        cost_twd=cost,
        today_spent=day_total,   # 全站今日累計（晨報+所有問答）
        daily_limit=limit,
    )
