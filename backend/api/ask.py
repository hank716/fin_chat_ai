"""即時查詢端點（M4）：Discord bot 把使用者問題轉來，grounded 在最新晨報作答。

流程：預算檢查 → 取最新報告 → grounded Gemini Q&A → 估算並記錄成本 → 回 answer+成本。
資料計算不用 Gemini（§25.1）；只有作答用。bot 只是薄轉接，Gemini/成本邏輯都在這。
"""
from __future__ import annotations

import logging
import re
import secrets

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ai import gemini_cache, gemini_client
from ai.errors import LLMError, LLMQuotaExceeded
from ai.gemini_client import GeminiBadRequest
from ai.llm_client import get_decision_llm
from ai.prompts import build_chat_system, build_qa_static_block, build_qa_variable_block
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


def _ask_gemini(rid: str, report: dict, variable_block: str) -> tuple[str, dict]:
    """Gemini 問答路徑（降級 / `LLM_DECISION_PROVIDER=gemini` 時用）。

    保留原本的明確快取（cachedContents）邏輯——那是 Gemini 專屬機制，與 Anthropic 的
    `cache_control` 完全不同（前者按存放時間計費、後者按讀寫次數），不能互換。
    """
    from config import settings

    static_block = build_qa_static_block(report)
    cache_name = gemini_cache.get_or_create_qa_cache(rid, static_block, settings.gemini_model_qa)
    if cache_name:
        try:
            # 明確快取命中：contents 只送變動尾段，靜態前綴已在快取內。
            return gemini_client.generate_text(
                variable_block, use_search=True, cached_content=cache_name
            )
        except GeminiBadRequest as exc:
            # 明確快取與本次請求（如 google_search tools）不相容 → 降級為完整 prompt（仍吃隱式快取）。
            logger.warning("明確快取不相容，降級為完整 prompt：%s", exc)
    return gemini_client.generate_text(static_block + variable_block, use_search=True)


class AskRequest(BaseModel):
    user_id: str
    question: str
    # 僅 Discord 討論串會帶：有值＝該串內帶短期對話記憶；無值（一般頻道）＝維持無狀態
    conversation_id: str | None = None


class AskResponse(BaseModel):
    answer: str
    report_id: str
    cost_twd: float
    today_spent: float
    daily_limit: float
    budget_exceeded: bool = False


def _admin_ok(token: str | None) -> bool:
    """帶有效 ADMIN_TOKEN（X-Admin-Token 標頭）→ 測試放行。未設定 ADMIN_TOKEN 一律不放行。"""
    from config import settings

    expected = settings.admin_token.strip()
    return bool(expected) and bool(token) and secrets.compare_digest(token, expected)


@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, x_admin_token: str | None = Header(default=None)) -> AskResponse:
    # ⚠️ 預算原子性不變式：check_budget() → record_cost() 之間**不得有 await/yield**。
    # 本 handler 全程為同步阻塞呼叫（含 gemini_client.generate_text、同步 redis），故同一 process
    # 內兩個 /ask 不會交錯，check-then-spend 對本地請求是原子的（家用單 worker 規模足夠）。
    # 若日後把下游改成 async await，這個不變式就會被打破而出現 TOCTOU 超支——屆時需改用
    # redis 原子操作（如 Lua / INCR 預扣）來守上限。跨 process（晨報排程）為軟上限、超支有界，可接受。
    ok, reason, spent, limit = tracker.check_budget()
    # 測試放行：帶有效 X-Admin-Token 時跳過上限，但**花費照常計入**（buckets 仍累加）。
    bypass = _admin_ok(x_admin_token)
    if not ok and not bypass:
        return AskResponse(
            answer=f"{reason}。請明天再試或調整上限。",
            report_id="", cost_twd=0.0, today_spent=spent, daily_limit=limit,
            budget_exceeded=True,
        )
    if not ok and bypass:
        logger.info("預算已達上限，但帶管理權杖放行測試（花費仍計入）：%s", reason)

    from config import settings

    # 意圖分類（最便宜模型）：先過濾掉與財務無關的閒聊，省下大 prompt 與 Google 搜尋 grounding。
    # 花費仍計入（分類也是一次 Gemini 呼叫，但 token 極少）；fail-open＝分類器出錯一律當財務題續走。
    intent_cost = 0.0
    if settings.enable_intent_filter:
        is_financial, intent_usage = gemini_client.classify_finance_intent(req.question)
        if intent_usage:
            intent_cost = tracker.cost_of_usage(
                intent_usage, settings.gemini_model_classifier, grounded=False
            )
            tracker.record_cost(intent_cost, provider="gemini")
        if not is_financial:
            logger.info("ask user=%s 意圖非財務 → 婉拒（省 token）分類成本=NT$%.4f",
                        req.user_id, intent_cost)
            return AskResponse(
                answer="我只回答市場／個股／總經等財經相關的問題，這題恐怕幫不上忙 🙏",
                report_id=morning_brief.latest_report_id() or "",
                cost_twd=intent_cost,
                today_spent=tracker.today_total(),
                daily_limit=limit,
            )

    rid = morning_brief.latest_report_id()
    report = morning_brief.load_report(rid) if rid else None
    if report is None:
        raise HTTPException(status_code=404, detail="尚無晨報可查詢，請先產生晨報")

    known = set((report.get("features", {}).get("tw", {}) or {}).get("stocks", {}).keys())
    on_demand = _ondemand_symbols(req.question, known)
    # 基本面（月營收 YoY/MoM）：對問題中的台股代號 on-demand 抓
    from data_sources.finmind_loader import FinMindBackoff
    from processor.fundamentals import build_fundamentals
    fundamentals: dict = {}
    for sym in _TW_CODE.findall(req.question)[:3]:
        try:
            fu = build_fundamentals(sym)
        except FinMindBackoff as exc:
            # FinMind 額度耗盡/封鎖：照常回答，只是缺基本面，不讓問答整個失敗。
            logger.warning("問答基本面抓取因 FinMind 退避中止: %s", exc)
            break
        if fu:
            fundamentals[sym] = fu
    # 討論串記憶（一般頻道 conversation_id=None → 無記憶）
    from chat import history
    hist = history.load(req.conversation_id) if req.conversation_id else None
    # 每題變動尾段（即時標的 + 基本面 + 討論串記憶 + 問題）。與之相對的「當日穩定前綴」
    # （規則+晨報+features）由各 provider 自行處理：Claude 放 system（可選 cache_control）、
    # Gemini 走 cachedContents。兩者機制不同，不要試圖共用。
    variable_block = build_qa_variable_block(
        req.question, on_demand=on_demand, fundamentals=fundamentals, history=hist
    )
    llm = get_decision_llm()
    try:
        if llm.name == "anthropic":
            # 決策層路徑（spec 022）：穩定前綴放 system（可選 cache_control），變動尾段放 user。
            # 即時事實由 Claude 自己的 web_search/web_fetch 補——**刻意不**先跑一次 Gemini 召回：
            # 召回層是「今日市場事件」導向（晨報的需求），對「2330 現在能不能買」這種即時
            # 提問幫助有限，卻要多付一次 Gemini 呼叫與延遲。晨報才是召回層的服務對象。
            answer, usage = llm.answer_question(
                build_chat_system(report), variable_block, cacheable=True,
            )
            model_used = settings.claude_model_chat
            grounded = False   # Anthropic 的搜尋計費走 usage.web_search_requests，非 Google grounding
        else:
            answer, usage = _ask_gemini(rid, report, variable_block)
            model_used = settings.gemini_model_qa
            grounded = True
    except LLMQuotaExceeded as exc:
        raise HTTPException(status_code=503, detail=f"LLM 配額用盡：{exc}") from exc
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=f"LLM 暫時無法使用：{exc}") from exc

    cost = tracker.cost_of_usage(usage, model_used, grounded=grounded)
    tracker.record_cost(cost, provider=tracker.provider_of(model_used))
    day_total = tracker.today_total()
    logger.info("ask user=%s tokens=%s cost=NT$%.4f 今日全站=NT$%.2f",
                req.user_id, usage, cost, day_total)
    core_answer = answer or "（無回應）"
    # Q&A 輕量 guardrail（WP3.2）：禁語 redact + 疑似不存在代號標注。允許集＝universe∪晨報已知∪即時查。
    import universe
    from guardrails import qa_guard
    allowed_symbols = set(universe.watchlist_symbols()) | known | set(on_demand.keys())
    core_answer, qa_report = qa_guard.scan_answer(core_answer, allowed_symbols)
    if not qa_report["clean"]:
        logger.warning("ask guardrail 攔截 user=%s 禁語=%s 疑似代號=%s",
                       req.user_id, qa_report["banned_phrases"], qa_report["unknown_symbols"])
    if req.conversation_id:           # 討論串才寫記憶；存乾淨答案（不含免責句）
        history.append(req.conversation_id, req.question, core_answer)
    answer = core_answer + \
        "\n\n⚠️ 以上方向/目標價/止損為技術面輔助參考，非保證、請自行判斷風險。"
    return AskResponse(
        answer=answer,
        report_id=rid,
        cost_twd=round(cost + intent_cost, 6),  # 本次總花費＝問答 + 意圖分類
        today_spent=day_total,   # 全站今日累計（晨報+所有問答）
        daily_limit=limit,
    )
