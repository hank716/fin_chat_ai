"""LLM 決策層抽象（spec 022-llm-tiering，憲章 2.0.0 Principle I）。

分層架構下的**決策 + 查證層**入口。廣度召回層（Gemini + google_search）在 `retrieval.py`，
兩者職責分離：召回層負責「找得到」，決策層負責「信不信」。

這一層原本是 32 行的空殼 Protocol，且 `morning_brief.py` 匯入 `get_llm_client` 卻從未呼叫
（死匯入）。spec 022 把它做成真的分派點：

    get_decision_llm() → AnthropicDecisionLLM（預設）| GeminiDecisionLLM（降級 / A-B 對照）

保留 Gemini 實作有兩個用途：(a) 決策層故障時的降級路徑——晨報是無人值守的每日排程，
不得因換供應商而變成可能整份失敗；(b) 同一份 features + facts 跑兩邊做品質對照。
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from config import settings

from . import claude_client, gemini_client, prompts
from .claude_client import FetchAttempt
from .schemas import BriefDraft, BriefResult

logger = logging.getLogger("ai-market-backend.llm")

Usage = dict[str, int]


class DecisionLLM(Protocol):
    """決策層契約。兩個方法對應晨報與問答兩條路徑。"""

    name: str

    def draft_brief(
        self, features: dict[str, Any], facts_json: str, calibration: str | None,
        *, frugal: bool = False,
    ) -> tuple[BriefDraft, Usage, list[FetchAttempt]]:
        """features + 待查證線索 + 回測校準 → (草稿, usage, 查證 attempts)。

        `frugal=True`＝預算降級模式（月餘額不足時由呼叫端指定）：關連網查證、降 effort。

        第三個回傳值讓報告層能用**工具行為**判定每則線索的查證結局（spec 023 FR-006），
        而不是採信模型自述的 verdict。不做查證的實作回空 list，呼叫端因此不必分支。
        """
        ...

    def answer_question(
        self, system_prompt: str, user_prompt: str, *, cacheable: bool,
    ) -> tuple[str, Usage]:
        """問答作答（純文字）。"""
        ...


class AnthropicDecisionLLM:
    """Claude 決策層：單次呼叫 + 連網查證。

    相對 Gemini 路徑少了一整個階段——Anthropic 沒有「schema 不能與 tools 並用」的限制，
    所以不需要那個純格式化的第②段（它在 Gemini 路徑存在的唯一理由就是繞過該限制）。
    """

    name = "anthropic"

    def draft_brief(
        self, features: dict[str, Any], facts_json: str, calibration: str | None,
        *, frugal: bool = False,
    ) -> tuple[BriefDraft, Usage, list[FetchAttempt]]:
        # 節儉模式：不給工具、不帶 facts pack、effort 降到最低。刻意**不是**「少查幾次」——
        # 部分查證比不查證更糟（看起來像查過了），所以要關就整個關，並在 system prompt 明講
        # 「不得引用 features 以外的外部事件」，否則模型會去呼叫不存在的工具。
        if frugal:
            logger.warning("晨報進入預算節儉模式：關閉連網查證、effort=low")
        tools = [] if frugal else claude_client.build_tools(
            settings.claude_brief_fetch_uses, settings.claude_brief_search_uses,
        )
        draft, usage, attempts = claude_client.generate_structured(
            BriefDraft,
            system=prompts.build_decision_system(verify=not frugal),
            user_prompt=prompts.build_decision_prompt(
                features, "[]" if frugal else facts_json, calibration,
            ),
            model=settings.claude_model_decision,
            tools=tools,
            # 與問答相反、預設開：連網查證的工具迴圈會把這個 ~55k 前綴在單一請求內重讀多輪，
            # 快取讀取 0.1× 正好打在成本主體上。
            # ⚠️ 節儉模式沒有工具＝只有一輪，前綴不會被重讀，此時 1.25× 的寫入是純虧 → 關掉。
            cacheable=settings.enable_claude_brief_prompt_cache and not frugal,
            # 晨報獨立一檔（見 config.claude_brief_effort）：thinking token 以 output 費率計價，
            # 這是輸出端的成本主閥。
            effort="low" if frugal else settings.claude_brief_effort,
        )
        return draft, usage, attempts  # type: ignore[return-value]

    def answer_question(
        self, system_prompt: str, user_prompt: str, *, cacheable: bool,
    ) -> tuple[str, Usage]:
        block: dict[str, Any] = {"type": "text", "text": system_prompt}
        # ⚠️ 預設**不**掛 cache_control。寫入付 1.25×(5m)/2×(1h)、讀取才 0.1×，
        # 5m 需 2 次、1h 需 3 次以上讀取才回本；chat 用量稀疏時每題都是「寫入後未被讀取
        # 就過期」＝純虧。詳見 config.enable_claude_prompt_cache 的註解。
        if cacheable and settings.enable_claude_prompt_cache:
            block["cache_control"] = {"type": "ephemeral", "ttl": settings.claude_cache_ttl}
        return claude_client.generate_answer(
            system_blocks=[block],
            user_prompt=user_prompt,
            model=settings.claude_model_chat,
            tools=claude_client.build_tools(
                settings.claude_chat_fetch_uses, settings.claude_chat_search_uses,
            ),
        )


class GeminiDecisionLLM:
    """Gemini 決策層——**降級路徑**，非預設。

    沿用既有兩段式（①PRO+搜尋寫分析稿 → ②Flash 格式化）。注意此路徑**不做查證**：
    Gemini 同時扮演召回與決策，沒有第二方稽核，這正是 spec 022 要解決的問題。
    只在 Claude 不可用時退到這裡，或用於 A-B 品質對照。
    """

    name = "gemini"

    def draft_brief(
        self, features: dict[str, Any], facts_json: str, calibration: str | None,
        *, frugal: bool = False,
    ) -> tuple[BriefDraft, Usage, list[FetchAttempt]]:
        # `frugal` 在這條路徑無對應旋鈕（Gemini 兩段式沒有 effort、搜尋是模型內建），
        # 收下但不使用——它本來就是降級路徑，單篇約 NT$14，不是成本敞口。
        result, research_usage, struct_usage = gemini_client.analyze_full_brief_grounded(
            features, calibration=calibration,
        )
        usage = {
            k: research_usage.get(k, 0) + struct_usage.get(k, 0)
            for k in ("input_tokens", "output_tokens", "cached_tokens", "tool_tokens")
        }
        # attempts 恆為空：這條路徑不做查證（沒有 web_fetch），不是「查了 0 次」而是
        # 「沒有查證這回事」。報告層據此把整篇排除在查證統計外，不污染成功率基準。
        return _result_to_draft(result), usage, []

    def answer_question(
        self, system_prompt: str, user_prompt: str, *, cacheable: bool,
    ) -> tuple[str, Usage]:
        return gemini_client.generate_text(
            system_prompt + "\n\n" + user_prompt,
            model=settings.gemini_model_qa,
            use_search=True,
        )


def _result_to_draft(result: BriefResult) -> BriefDraft:
    """BriefResult → BriefDraft，讓降級路徑回傳與主路徑相同的型別。

    Gemini 路徑不做查證，故 `fact_checks` 為空——這在報告裡是誠實的訊號：
    看到 fact_checks 空且 decision_provider 為 gemini-fallback，就知道那天沒查證過。
    """
    payload = result.model_dump(mode="json")
    payload["fact_checks"] = []
    for key in ("tw_watchlist", "tw_caution"):
        for item in payload.get(key) or []:
            for ml_field in ("risk_score", "conviction_score", "size_weight"):
                item.pop(ml_field, None)
    for item in payload.get("news_digest") or []:
        item.pop("tier", None)
        item.pop("provider", None)
    return BriefDraft.model_validate(payload)


def get_decision_llm(provider: str | None = None) -> DecisionLLM:
    """依設定回傳決策層實作。未知的 provider 名稱 fail-fast，不要靜默走錯供應商。"""
    name = (provider or settings.llm_decision_provider or "anthropic").strip().lower()
    if name == "anthropic":
        return AnthropicDecisionLLM()
    if name == "gemini":
        return GeminiDecisionLLM()
    raise ValueError(
        f"未知的 LLM_DECISION_PROVIDER={name!r}（可用：anthropic / gemini）"
    )
