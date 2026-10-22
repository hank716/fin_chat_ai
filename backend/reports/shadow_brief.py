"""同日影子對照（spec 026 US4）：主晨報發布後，以同一份輸入再跑一次 OpenAI 決策層。

目的：累積 5–10 個交易日「同一天、同一份 features + facts pack」的配對資料，比較 Claude 與
OpenAI 的成本、裁決率、guardrail 違規數（`reports.provider_compare`），再決定要不要切換預設。

## 三條硬規則

1. **只存檔、不發布**：寫到 `REPORTS_DIR/shadow/<report_id>_openai.json`。所有讀報告的地方
   （網頁、Discord、Supabase、retention、backtest）都只 glob 頂層的 `morning_*.json`，子目錄
   天然隔離（SC-007）。檔名仍以 `morning_` 開頭，是為了讓 `verification_stats` 指到 shadow/
   目錄就能直接重用。
2. **不得影響主晨報**：在發布之後才跑；整段 try/except；失敗只寫 error 檔。
3. **不得把之後的主晨報推進節儉模式**：跑之前先扣掉預估花費，月餘額仍須 ≥ 降級門檻
   （FR-017a）。影子花費照實記入 openai 桶與彙總桶——預算閘門看得到。

影子失敗時**不跑 Gemini 降級**：影子不發布，降級版沒有比較價值又多花錢。
試驗結束後整個模組可以直接刪掉，`morning_brief` 只有一個呼叫點。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from cost import tracker

logger = logging.getLogger("ai-market-backend.shadow")

SHADOW_PROVIDER = "openai"


def shadow_dir() -> Path:
    from reports import morning_brief

    return morning_brief.REPORTS_DIR / "shadow"


def shadow_path(report_id: str) -> Path:
    return shadow_dir() / f"{report_id}_{SHADOW_PROVIDER}.json"


def should_run_shadow(
    today: date, *, frugal: bool, primary_provider: str,
) -> tuple[bool, str | None]:
    """回 (要不要跑, 不跑的原因)。原因會落地到主報告的 `shadow.reason`。"""
    until = settings.openai_shadow_until
    if until is None:
        return False, "disabled"
    if today > until:
        return False, "expired"
    if frugal:
        # 主晨報都已經在省錢了，再多花一筆做實驗說不過去；而且節儉版沒有查證，不可比
        return False, "frugal"
    if primary_provider in (SHADOW_PROVIDER, "gemini-fallback"):
        # 主路徑本身就是 openai＝沒有對照組；主路徑降級＝那天的對照組不是 Claude
        return False, f"primary_is_{primary_provider}"
    reserve = float(settings.brief_degrade_reserve_twd)
    estimate = float(settings.openai_shadow_cost_estimate_twd)
    if tracker.month_remaining() - estimate < reserve:
        return False, "budget"
    return True, None


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_shadow(
    feats: dict[str, Any],
    facts: Any,
    calibration_text: str,
    *,
    report_id: str,
    report_date: str,
    main_retrieval_twd: float,
) -> dict[str, Any]:
    """跑一次影子並寫檔。回主報告 `shadow` 欄位要放的指標：{status, reason, path}。永不 raise。"""
    from ai.errors import LLMError
    from ai.llm_client import get_decision_llm
    from guardrails.verify import run_guardrails
    from reports import morning_brief as mb
    from reports.markdown_builder import build_markdown

    path = shadow_path(report_id)
    model = settings.openai_model_decision
    base: dict[str, Any] = {
        "report_id": report_id,
        "report_date": report_date,
        "shadow_of": report_id,
        "decision_provider": SHADOW_PROVIDER,
        "decision_model": model,
        "decision_model_version": None,
        "facts": [e.model_dump() for e in getattr(facts, "events", [])],
        "error": None,
    }

    def _cost_block(usage: dict[str, int], decision_twd: float,
                    verification: dict[str, Any] | None) -> dict[str, Any]:
        # 形狀對齊主報告的 cost 區塊：verification_stats / provider_compare 直接讀得懂
        return {
            "decision_provider": SHADOW_PROVIDER,
            "decision_model": model,
            "frugal_mode": False,
            "decision_twd": round(decision_twd, 4),
            # 召回共用、只付一次：比較時兩邊都加同一個召回成本才公平
            "retrieval_twd": round(float(main_retrieval_twd or 0.0), 4),
            "tokens": dict(usage),
            "verification": verification or {},
        }

    def _record(usage: dict[str, int]) -> float:
        cost = tracker.cost_of_usage(usage, model) if usage else 0.0
        if cost:
            tracker.record_cost(round(cost, 4), provider=SHADOW_PROVIDER)
        return cost

    try:
        llm = get_decision_llm(SHADOW_PROVIDER)
        try:
            draft, usage, attempts = llm.draft_brief(
                feats, facts.to_prompt_json(), calibration_text or None,
            )
        except LLMError as exc:
            usage = dict(getattr(exc, "usage", None) or {})
            attempts = list(getattr(exc, "fetch_attempts", None) or [])
            cost = _record(usage)
            verification, _ = mb._verification_stats(
                facts, [], usage, attempts, active=True, provider=SHADOW_PROVIDER,
            )
            _write(path, {**base, "cost": _cost_block(usage, cost, verification),
                          "fact_checks": [], "error": f"{type(exc).__name__}: {exc}"[:500]})
            logger.warning("影子對照（openai）失敗，主晨報不受影響：%s", exc)
            return {"status": "error", "reason": type(exc).__name__, "path": str(path)}

        cost = _record(usage)
        result = mb._draft_to_result(draft)
        fact_checks = [fc.model_dump() for fc in draft.fact_checks]
        mb._backfill_caution(result, feats)
        verification, clue_outcomes = mb._verification_stats(
            facts, fact_checks, usage, attempts, active=True, provider=SHADOW_PROVIDER,
        )
        result, guardrail = run_guardrails(result, feats, clue_outcomes=clue_outcomes)
        cost_block = _cost_block(usage, cost, verification)
        markdown = build_markdown(
            result, generated_at=datetime.now(ZoneInfo(settings.tz)), cost=cost_block,
        )
        _write(path, {
            **base,
            "decision_model_version": getattr(llm, "last_model_version", None),
            "cost": cost_block,
            "fact_checks": fact_checks,
            "verification": verification,
            "guardrail": guardrail,
            "result": result.model_dump(mode="json"),
            "markdown": markdown,
        })
        logger.info("影子對照（openai）完成 NT$%.4f → %s", cost, path)
        return {"status": "ok", "reason": None, "path": str(path)}
    except Exception as exc:  # noqa: BLE001 — 影子任何錯誤都不得往上冒
        logger.warning("影子對照非預期錯誤（主晨報不受影響）：%s", exc)
        try:
            _write(path, {**base, "error": f"{type(exc).__name__}: {exc}"[:500]})
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "reason": type(exc).__name__, "path": str(path)}


def maybe_run_shadow(
    feats: dict[str, Any],
    facts: Any,
    calibration_text: str,
    *,
    report_id: str,
    report_date: str,
    main_retrieval_twd: float,
    frugal: bool,
    primary_provider: str,
) -> dict[str, Any]:
    """`morning_brief` 唯一的呼叫點：判斷要不要跑，要跑就跑。永不 raise。"""
    try:
        today = datetime.now(ZoneInfo(settings.tz)).date()
        ok, reason = should_run_shadow(today, frugal=frugal, primary_provider=primary_provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning("影子對照前置檢查失敗：%s", exc)
        return {"status": "error", "reason": type(exc).__name__, "path": None}
    if not ok:
        if reason != "disabled":
            logger.info("影子對照跳過：%s", reason)
        return {"status": "skipped", "reason": reason, "path": None}
    return run_shadow(
        feats, facts, calibration_text, report_id=report_id, report_date=report_date,
        main_retrieval_twd=main_retrieval_twd,
    )
