"""即時查詢端點（M4）：Discord bot 把使用者問題轉來，grounded 在最新晨報作答。

流程：預算檢查 → 取最新報告 → grounded Gemini Q&A → 估算並記錄成本 → 回 answer+成本。
資料計算不用 Gemini（§25.1）；只有作答用。bot 只是薄轉接，Gemini/成本邏輯都在這。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai import gemini_client
from ai.gemini_client import GeminiError, GeminiQuotaExceeded
from ai.prompts import build_qa_prompt
from cost import tracker
from reports import morning_brief

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
    ok, spent, limit = tracker.check_budget(req.user_id)
    if not ok:
        return AskResponse(
            answer=f"今日 AI 查詢額度已用完（NT${spent:.1f} / NT${limit:.0f}），明天再試或調整上限。",
            report_id="", cost_twd=0.0, today_spent=spent, daily_limit=limit,
            budget_exceeded=True,
        )

    rid = morning_brief.latest_report_id()
    report = morning_brief.load_report(rid) if rid else None
    if report is None:
        raise HTTPException(status_code=404, detail="尚無晨報可查詢，請先產生晨報")

    prompt = build_qa_prompt(req.question, report)
    try:
        answer, usage = gemini_client.generate_text(prompt)
    except GeminiQuotaExceeded as exc:
        raise HTTPException(status_code=503, detail=f"Gemini 配額用盡：{exc}") from exc
    except GeminiError as exc:
        raise HTTPException(status_code=503, detail=f"Gemini 暫時無法使用：{exc}") from exc

    cost = tracker.estimate_cost_twd(usage["input_tokens"], usage["output_tokens"])
    total = tracker.record_cost(req.user_id, cost)
    logger.info("ask user=%s tokens=%s cost=NT$%.4f total=NT$%.2f",
                req.user_id, usage, cost, total)
    return AskResponse(
        answer=answer or "（無回應）",
        report_id=rid,
        cost_twd=cost,
        today_spent=total,
        daily_limit=limit,
    )
