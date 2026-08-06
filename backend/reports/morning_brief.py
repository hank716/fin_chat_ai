"""每日跨市場晨報管線（M1 step 5；LLM 分層見 spec 022）。

跑完整黃金路徑：yfinance 抓 → parquet 落地 → intermarket features
→ ①Gemini 廣度召回待查證線索 → ②Claude 決策 + web_fetch 逐條查證 → 本地 ML 打分/融合/sizing
→ md/json/copy-for-ai → 存檔 storage/reports/{id}.json|.md。

②失敗時退回 Gemini 兩段式（無人值守的每日排程不得因換供應商而整份失敗）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import universe
from config import settings
from ai import retrieval
from ai.errors import LLMError
from ai.llm_client import get_decision_llm
from ai.schemas import BriefResult
from cost import tracker
from data_sources import news_loader, yfinance_loader
from data_sources.backfill_tw_market import backfill_market
from data_sources.ingest import _dq_filter
from guardrails.verify import run_guardrails
from processor.intermarket_features import build_intermarket_features
from processor.tw_features import build_tw_features
from reports.copy_for_ai_builder import build_copy_for_ai
from reports.json_builder import build_report_dict
from reports.markdown_builder import build_markdown
from storage import local_store

logger = logging.getLogger("ai-market-backend.morning_brief")

REPORTS_DIR = Path(settings.local_storage_path) / "reports"
REPORT_ID_RE = re.compile(r"^morning_\d{8}_\d{6}$")


def _news_focus_symbols(tw_feats: dict[str, Any], cap: int = 12) -> list[str]:
    """從 movers 挑出最值得看新聞的台股（漲跌幅 + 外資買賣超前段），去重。"""
    out: list[str] = []
    movers = tw_feats.get("movers", {})
    for key in ("top_gainers_5d", "top_losers_5d", "top_foreign_buy_5d", "top_foreign_sell_5d"):
        for item in movers.get(key, []):
            sym = item.get("symbol")
            if sym and sym not in out:
                out.append(sym)
    return out[:cap]


def _caution_signals(entry: dict[str, Any]) -> list[str]:
    """從個股 features 萃取可佐證『偏空/要注意』的實際訊號（皆為真實數值，不外推）。"""
    sig: list[str] = []
    r5 = entry.get("return_5d_pct")
    if isinstance(r5, (int, float)) and r5 < 0:
        sig.append(f"近5日{r5:.1f}%")
    vs = entry.get("vs_index_20d_pct")
    if isinstance(vs, (int, float)) and vs < 0:
        sig.append(f"相對大盤{vs:.1f}%")
    if entry.get("above_ma20") is False:
        sig.append("跌破MA20")
    streak = entry.get("foreign_net_streak")
    if isinstance(streak, int) and streak < 0:
        sig.append(f"外資連賣{abs(streak)}日")
    f5 = entry.get("foreign_net_buy_5d_lots")
    if isinstance(f5, (int, float)) and f5 < 0:
        sig.append(f"外資5日賣超{abs(int(f5)):,}張")
    smr = entry.get("short_margin_ratio_pct")
    if isinstance(smr, (int, float)) and smr >= 30:
        sig.append(f"資券比{smr:.0f}%偏高")
    return sig


def _backfill_caution(result: Any, features: dict[str, Any], want: int = 5) -> int:
    """偏空清單不足時，從 movers 以實際數據補齊，確保晨報永遠有『偏空/要注意標的』段落。

    模型在偏多盤勢常整段略過 tw_caution（rare bug）。這裡用 movers 跌幅/資券比/相對弱勢/外資賣超排行
    補列——符號都出自 movers（必在 features.tw.stocks，可過 guardrail），signals 全為真實數值不捏造。
    回補上的檔數。
    """
    from ai.schemas import WatchItem

    if len(result.tw_caution) >= want:
        return 0
    tw = features.get("tw", {}) or {}
    stocks = tw.get("stocks", {}) or {}
    movers = tw.get("movers", {}) or {}
    have = {w.symbol for w in result.tw_caution}
    added = 0
    for key in ("top_losers_5d", "top_below_index_20d", "top_short_margin_ratio", "top_foreign_sell_5d"):
        for it in movers.get(key, []):
            if len(result.tw_caution) >= want:
                break
            sym = it.get("symbol")
            if not sym or sym in have:
                continue
            entry = stocks.get(sym, {})
            sigs = _caution_signals(entry)
            if not sigs:  # 找不到可佐證的偏空訊號就不硬湊
                continue
            have.add(sym)
            added += 1
            result.tw_caution.append(WatchItem(
                symbol=sym,
                name=entry.get("name") or it.get("name") or sym,
                sector=entry.get("sector") or it.get("sector"),
                thesis="技術面偏空：" + "、".join(sigs[:3])
                + "（系統依排行自動補列，建議搭配當日量價與消息確認）",
                signals=sigs,
                uncertainty="自動補列、未經 AI 個別研判；目標價/止損請依技術區間自行評估。",
            ))
    if added:
        logger.info("tw_caution 由模型回傳 %d 檔，已用 movers 實際數據補至 %d 檔",
                    len(result.tw_caution) - added, len(result.tw_caution))
    return added


def _build_combined_features(refresh_tw: bool) -> tuple[dict[str, Any], dict[str, int]]:
    """組出餵 Gemini 的合併 features：美股+加密 / 台股+籌碼 / 聚焦新聞 / 跨市場連動。"""
    # 1) 美股指數 + BTC + 大盤 TWII → DQ → parquet
    landed: dict[str, int] = {}
    for market, rows in yfinance_loader.fetch_intermarket().items():
        res = local_store.write_prices(_dq_filter(rows), market)
        landed[market] = res["symbols"]

    # 2) 台股全市場刷新（近 3 交易日，TWSE/TPEx 單日端點，快）
    if refresh_tw:
        try:
            backfill_market(days=3)
        except Exception as exc:  # noqa: BLE001 — 刷新失敗仍用既有落地資料產報告
            logger.warning("台股每日刷新失敗，沿用既有資料: %s", exc)

    us_crypto = build_intermarket_features()
    tw = build_tw_features()
    # FinMind 新聞為單日語意：用資料日(as_of)與 today 當 start_date 才拿得到最新新聞
    news = news_loader.fetch_news(
        _news_focus_symbols(tw), as_of=tw.get("as_of"), per_symbol=2
    )

    combined = {
        "as_of": tw.get("as_of") or us_crypto.get("as_of"),
        "us_crypto": us_crypto,
        "tw": tw,
        "news": news,
        "linkage": universe.us_to_tw_linkage(),
    }
    return combined, landed


def _candidate_list(result: Any, feats: dict[str, Any]) -> list[dict[str, Any]]:
    """組出模型打分用的候選清單（symbol + 當日 features + 偏多/偏空）。

    並把當前大盤 regime[9]（趨勢/波動）注入每檔 stock_entry，讓 featurize 與訓練端定義一致
    （regime 是市場級、同日對所有股相同；訓練端在 big 欄、serve 端在這裡注入）。
    """
    from reports import training_set

    tw = feats.get("tw", {}) or {}
    stocks = tw.get("stocks", {}) or {}
    movers = tw.get("movers", {}) or {}
    # conviction[5]：候選命中幾個 movers 清單（對齊 training_set 的 component 清單）＝訊號共振數。
    _bull_lists = ("top_gainers_5d", "top_foreign_buy_5d")
    _bear_lists = ("top_losers_5d", "top_foreign_sell_5d", "top_short_margin_ratio", "top_below_index_20d")
    bull_sets = [{it["symbol"] for it in (movers.get(k) or [])} for k in _bull_lists]
    bear_sets = [{it["symbol"] for it in (movers.get(k) or [])} for k in _bear_lists]
    try:
        regime = {k: v for k, v in training_set.current_market_regime().items() if v is not None}
    except Exception:  # noqa: BLE001 — regime 取得失敗不阻斷打分（缺值 HistGBT 原生吃）
        regime = {}
    out: list[dict[str, Any]] = []
    for side, lst in (("watchlist", result.tw_watchlist), ("caution", result.tw_caution)):
        sets = bull_sets if side == "watchlist" else bear_sets
        for w in lst:
            se = dict(stocks.get(w.symbol, {}))
            se.update(regime)
            se["conviction"] = sum(1 for s in sets if w.symbol in s)
            out.append({"symbol": w.symbol, "side": side, "stock_entry": se})
    return out


def _apply_risk_scores(result: Any, feats: dict[str, Any]) -> dict[str, float]:
    """用本地回撤風險模型打「未來深跌機率」：標記偏多高風險 + 強化避雷側排序（與方向分離）。

    避雷側(caution)依風險由高到低排（最該避的在前）；偏多清單**不重排**，只把 risk_score 寫進
    WatchItem 供前端標 ⚠。無模型/未過 gate 則原序不動。
    """
    from reports import strategy_calibration

    scores = strategy_calibration.score_risk(_candidate_list(result, feats))
    if scores:
        for w in result.tw_watchlist:
            w.risk_score = scores.get(w.symbol)        # 標記用，不重排偏多
        result.tw_caution.sort(key=lambda w: scores.get(w.symbol, 0.0), reverse=True)
    return scores


def _apply_meta_scores(result: Any, feats: dict[str, Any]) -> dict[str, float]:
    """meta-labeling 模型打「這筆訊號會成功的機率」→ sizing/過濾（不重排方向）。

    把 P 寫進 WatchItem.conviction_score（前端標把握度/低把握降權）；未過 gate / 無模型則原序不動。
    刻意不排序——meta 拚『該不該下手』，方向排序交給 edge/rank（目前皆撞牆關閉）。
    """
    from reports import strategy_calibration

    scores = strategy_calibration.score_meta(_candidate_list(result, feats))
    if scores:
        for w in result.tw_watchlist:
            w.conviction_score = scores.get(w.symbol)
        for w in result.tw_caution:
            w.conviction_score = scores.get(w.symbol)
    return scores


def _apply_sizing(result: Any, feats: dict[str, Any]) -> dict[str, float]:
    """把 risk_score×conviction_score 合成偏多書部位權重 → WatchItem.size_weight（long-only、和≈1）。

    僅在離線回測證明某方案淨贏等權時啟用（strategy_calibration.sizing_plan 過 gate 才回方案名）；
    否則不填＝前端退回等權。需先跑過 _apply_risk_scores/_apply_meta_scores（risk_score/conviction_score 已填）。
    """
    from reports import strategy_calibration

    scheme = strategy_calibration.sizing_plan()
    scalar, market_info = strategy_calibration.market_exposure_scalar()
    wl = result.tw_watchlist
    # 兩道 gate 都不動（無 sizing 方案 + 無曝險縮放）→ 維持今日行為。
    if not wl or (not scheme and scalar >= 0.999):
        return {}, market_info
    stocks = (feats.get("tw", {}) or {}).get("stocks", {}) or {}
    items = [{"meta_p": w.conviction_score, "risk_p": w.risk_score,
              "vol": (stocks.get(w.symbol, {}) or {}).get("volatility_20d_pct")} for w in wl]
    base = strategy_calibration._size_weights(items, scheme or "equal")  # 無方案則等權，僅供曝險縮放
    out: dict[str, float] = {}
    for w, wt in zip(wl, base):
        w.size_weight = round(float(wt) * scalar, 4)                     # 乘市場曝險係數（Σ=scalar、其餘現金）
        out[w.symbol] = w.size_weight
    return out, market_info




def _run_backtest_loop() -> dict[str, Any]:
    """回測迴圈（純本地、零 LLM 成本）：回測已到期晨報 → 重建校準 → 訓練 edge 模型。

    全程 guarded：任何失敗都只記 log，**絕不影響晨報產出**。回最新校準 summary 供顯示。
    """
    from reports import backtest, strategy_calibration, training_set

    summary: dict[str, Any] = {}
    try:
        from data_sources import taifex_loader        # TAIFEX P/C 日常增量（市場恐慌 gauge；guarded）
        taifex_loader.refresh_recent()
    except Exception as exc:  # noqa: BLE001
        logger.warning("TAIFEX 增量失敗（不影響晨報）: %s", exc)
    try:
        from processor import adj_factors             # 除權息因子表增量（分桶輪替；spec 017；guarded）
        adj_factors.refresh_recent()
    except Exception as exc:  # noqa: BLE001
        logger.warning("除權息因子增量失敗（不影響晨報）: %s", exc)
    try:
        backtest.run_due_evaluations()
        training_set.build_if_stale()                  # 歷史回放訓練集（過舊才重建，便宜）
        strategy_calibration.train_edge_model()        # 各窗(5/20)訓練→寫 edge_meta
        strategy_calibration.train_risk_model()        # 回撤風險模型（與方向並存）→寫 risk_meta
        strategy_calibration.train_rank_model()        # 報酬 rank 模型（殘差方向，rank-IC）→寫 rank_meta
        strategy_calibration.train_rank_model(band="smallcap")  # WP2.3 小型股帶 rank 模型（[5M,50M)，rank-IC 較強）
        strategy_calibration.train_meta_model()         # meta-labeling（該不該下手，triple-barrier）→寫 meta_meta
        strategy_calibration.backtest_market_regime()    # 市場恐慌 regime 回測（TAIFEX P/C）→寫 market_regime gate
        summary = strategy_calibration.rebuild()       # 再彙整：calibration 才會帶到最新 edge 狀態
        strategy_calibration.evaluate_effectiveness()  # 成效量測（把「準不準」變數字）
    except Exception as exc:  # noqa: BLE001
        logger.warning("回測/校準迴圈失敗（不影響晨報）: %s", exc)
    return summary


def _fetch_facts(feats: dict[str, Any]) -> tuple[retrieval.FactsPack, dict[str, int]]:
    """廣度召回層：Gemini + google_search 產待查證線索（spec 022 WP2）。

    關掉 `enable_facts_pack` 就回空——決策層仍可只憑 features 產出晨報，只是少了外部事件。
    """
    if not settings.enable_facts_pack:
        return retrieval.FactsPack(), {}
    as_of = str(feats.get("as_of") or datetime.now(ZoneInfo(settings.tz)).date())
    return retrieval.fetch_facts(as_of)


def _decide_brief(
    feats: dict[str, Any], facts: retrieval.FactsPack, calibration_text: str,
) -> tuple[BriefResult, dict[str, int], str, list[dict[str, Any]]]:
    """決策 + 查證層：回 (BriefResult, usage, provider, fact_checks)。

    主路徑是 Claude 單次呼叫（含 web_fetch 查證）；任何 LLMError（配額/過載/refusal/
    schema 不符）都退回 Gemini 兩段式。**降級是必要的**：晨報每天無人值守自動跑，
    不能因為換了決策供應商就變成有機率整份產不出來。

    降級時 `fact_checks` 為空——這在報告裡是誠實的訊號：看到 provider=gemini-fallback
    且 fact_checks 空，就知道那天的外部事件沒有經過查證。
    """
    primary = get_decision_llm()
    try:
        draft, usage = primary.draft_brief(feats, facts.to_prompt_json(), calibration_text or None)
        return (
            _draft_to_result(draft),
            usage,
            primary.name,
            [fc.model_dump() for fc in draft.fact_checks],
        )
    except LLMError as exc:
        if primary.name == "gemini":
            raise  # 已經是降級路徑本身，沒有更下層可退
        logger.warning("決策層(%s)失敗，降級回 Gemini 兩段式：%s", primary.name, exc)

    fallback = get_decision_llm("gemini")
    draft, usage = fallback.draft_brief(feats, facts.to_prompt_json(), calibration_text or None)
    return _draft_to_result(draft), usage, "gemini-fallback", []


def _draft_to_result(draft: Any) -> BriefResult:
    """BriefDraft → BriefResult。

    draft 刻意不含 risk_score / conviction_score / size_weight（那三個由後面的本地 ML
    打分階段填），轉過來時它們就是 None，正好是 BriefResult 的預設值。
    """
    payload = draft.model_dump(mode="json")
    payload.pop("fact_checks", None)   # 稽核用，不進 BriefResult（另外落地到報告 JSON）
    return BriefResult.model_validate(payload)


def generate_morning_brief(
    raw_query: str | None = None, *, refresh_tw: bool = True,
    push_discord: bool = False, publish: bool = False,
) -> dict[str, Any]:
    generated_at = datetime.now(ZoneInfo(settings.tz))

    feats, landed = _build_combined_features(refresh_tw)

    # 策略自動修正（回灌端）：注入「過去預估的回測校準」讓模型自我修正選股傾向。讀本機檔，便宜。
    from reports import strategy_calibration
    calibration_text = strategy_calibration.build_calibration_block()
    if calibration_text:
        logger.info("注入策略校準（%d 字）至晨報 prompt", len(calibration_text))

    # ── LLM 分層（spec 022）──
    # ① 廣度召回：Gemini + google_search 產「待查證線索」（不做分析、不挑股）
    facts, facts_usage = _fetch_facts(feats)
    # 刻意**不**塞進 feats["web_context"]：那樣同一份線索會在 prompt 裡出現兩次
    # （features JSON 內一次、facts pack 一次），白燒 token。稽核用的副本走 report["facts"]。

    # ② 決策 + 查證：Claude 單次呼叫，用 web_fetch 逐條開啟上面的 URL 核對後才採信。
    #    失敗時退回 Gemini 兩段式——晨報是無人值守的每日排程，不得因換供應商而整份失敗。
    result, decision_usage, provider, fact_checks = _decide_brief(feats, facts, calibration_text)

    facts_cost = (
        tracker.cost_of_usage(facts_usage, settings.gemini_model_qa, grounded=True)
        if facts_usage else 0.0
    )
    decision_model = (
        settings.claude_model_decision if provider == "anthropic" else settings.gemini_model_brief
    )
    brief_cost = round(
        facts_cost + tracker.cost_of_usage(decision_usage, decision_model),
        4,
    )
    usage = {
        "input_tokens": facts_usage.get("input_tokens", 0) + decision_usage.get("input_tokens", 0),
        "output_tokens": (facts_usage.get("output_tokens", 0)
                          + decision_usage.get("output_tokens", 0)),
    }
    tracker.record_cost(brief_cost)

    # 偏空清單偶爾被模型整段略過 → 用 movers 實際數據補齊（在 guardrail 前，符號必在資料範圍內）
    _backfill_caution(result, feats)

    month_total = tracker.month_total()
    monthly_limit = float(settings.monthly_cost_limit_twd)
    cost_info = {
        "brief_twd": brief_cost,
        "tokens": usage,
        "month": tracker.current_month(),
        "month_total_twd": month_total,                  # 全站本月累計（晨報+所有問答）
        "day_total_twd": tracker.today_total(),          # 全站今日累計
        "monthly_limit_twd": monthly_limit,
        # 晨報**不受** check_budget() 攔截（那只擋 /ask），所以它可能把月額度吃到見底、
        # 讓接下來整個月的問答全被擋。這是「晨報優先」的刻意取捨，但不該無聲發生——
        # 把剩餘額度攤在報告上，超支前就看得見。
        "month_remaining_twd": round(monthly_limit - month_total, 4),
        "decision_provider": provider,
    }
    logger.info("晨報 LLM 花費 NT$%.4f（provider=%s 本月累計 NT$%.2f / 上限 NT$%.0f）",
                brief_cost, provider, month_total, monthly_limit)
    if cost_info["month_remaining_twd"] <= 0:
        logger.warning("本月 LLM 額度已用罄（NT$%.2f/NT$%.0f）——問答將被擋，晨報仍會續跑",
                       month_total, monthly_limit)

    # guardrail：驗證未超出資料範圍、無捏造/禁語，清理後再 render
    result, guardrail = run_guardrails(result, feats)
    logger.info("guardrail passed=%s errors=%s warnings=%s",
                guardrail["passed"], guardrail["error_count"], guardrail["warning_count"])

    # 策略自動修正（打分端）：risk 標記/避雷排序、meta 標把握度、方向 edge/rank/qlib 融合後重排一次。
    edge_scores: dict[str, float] = {}
    rank_scores: dict[str, float] = {}
    qlib_scores: dict[str, float] = {}
    risk_scores: dict[str, float] = {}
    meta_scores: dict[str, float] = {}
    fused_scores: dict[str, float] = {}
    fusion_weights: dict[str, float] = {}
    # 回撤風險：標記偏多高風險 + 強化避雷側排序（guarded，無模型則不動）
    try:
        risk_scores = _apply_risk_scores(result, feats)
    except Exception as exc:  # noqa: BLE001
        logger.warning("風險打分/標記失敗（不影響晨報）: %s", exc)
    # Meta-labeling：標把握度（供 sizing/過濾，不重排方向；guarded）
    try:
        meta_scores = _apply_meta_scores(result, feats)
    except Exception as exc:  # noqa: BLE001
        logger.warning("meta 打分失敗（不影響晨報）: %s", exc)
    # 方向融合（WP2.2 / spec 019）：edge/rank/qlib 過各自 gate → z-score 加權平均 → 只重排偏多一次
    # （取代原 edge→rank→qlib 逐一 sort 的 last-writer-wins；全不過 gate 則不動）。
    try:
        from reports import strategy_calibration as _sc
        fused_scores, fusion_weights, _components = _sc.fuse_scores(_candidate_list(result, feats))
        edge_scores = _components.get("edge", {})
        rank_scores = _components.get("rank", {})
        qlib_scores = _components.get("qlib", {})
        if fused_scores:
            result.tw_watchlist.sort(key=lambda w: fused_scores.get(w.symbol, 0.0), reverse=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("方向融合/重排失敗（不影響晨報）: %s", exc)
    # 部位 sizing + 市場曝險覆蓋：risk×meta 合成偏多書權重 ×市場恐慌曝險係數（guarded；兩道 gate 不過則不動）
    size_weights: dict[str, float] = {}
    market_fear: dict[str, Any] = {}
    try:
        size_weights, market_fear = _apply_sizing(result, feats)
    except Exception as exc:  # noqa: BLE001
        logger.warning("sizing/曝險失敗（不影響晨報）: %s", exc)

    # 回測迴圈：回測已到期的過去晨報 → 重建校準 → 訓練模型（本機運算、零 LLM 花費）
    backtest_summary = _run_backtest_loop()

    # 4) builders
    report = build_report_dict(result, generated_at=generated_at, raw_query=raw_query)
    report["guardrail"] = guardrail
    report["cost"] = cost_info
    report["decision_provider"] = provider
    # 召回層線索與逐條查證結果（spec 022 稽核閉環）。fact_checks 的
    # contradicted/unverifiable 比例＝「召回層有沒有在胡說」的每日可量測數字。
    report["facts"] = [e.model_dump() for e in facts.events]
    report["fact_checks"] = fact_checks
    if edge_scores:
        report["edge_scores"] = edge_scores
    if risk_scores:
        report["risk_scores"] = risk_scores
    if rank_scores:
        report["rank_scores"] = rank_scores
    if qlib_scores:
        report["qlib_scores"] = qlib_scores
    if meta_scores:
        report["meta_scores"] = meta_scores
    if fused_scores:
        report["fused_scores"] = fused_scores       # WP2.2：方向融合後的排序分數
    if fusion_weights:
        report["fusion_weights"] = fusion_weights   # 各方向模型的正規化融合權重（超 gate 幅度）
    if size_weights:
        report["size_weights"] = size_weights
    if market_fear:
        report["market_fear"] = market_fear
    if calibration_text:
        report["calibration_injected"] = calibration_text
    if backtest_summary:
        report["backtest_summary"] = backtest_summary
    report_id = f"morning_{generated_at:%Y%m%d_%H%M%S}"
    report["report_id"] = report_id
    report["report_date"] = generated_at.date().isoformat()  # 晨報日期(今日)，有別於資料日期
    report["features"] = feats
    report["landed_symbols"] = landed
    report["markdown"] = build_markdown(
        result, generated_at=generated_at, raw_query=raw_query, cost=cost_info
    )
    report["copy_for_ai"] = build_copy_for_ai(
        result, generated_at=generated_at, raw_query=raw_query
    )

    # 5) 存檔
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / f"{report_id}.json"
    md_path = REPORTS_DIR / f"{report_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(report["markdown"], encoding="utf-8")
    logger.info("morning brief %s 產生完成 (landed=%s)", report_id, landed)

    # 6) Discord 推送
    pushed = False
    if push_discord:
        from notify.discord import send_daily_summary
        pushed = send_daily_summary(report)

    # 7) 發布：pCloud 冷備份 + Supabase report_index 暖索引
    if publish:
        from publish import pcloud_backup, supabase_publish
        pcloud_paths = pcloud_backup.backup_report(json_path, md_path)
        supabase_publish.publish_report_index(
            report, pcloud_paths=pcloud_paths, discord_pushed=pushed
        )
        # 8) 保留/清理：守本機容量預算（舊報告已備份 pCloud，可回補）
        try:
            from storage.retention import enforce_retention
            enforce_retention()
        except Exception as exc:  # noqa: BLE001
            logger.warning("retention 失敗: %s", exc)

    return report


def _safe_path(report_id: str, suffix: str) -> Path | None:
    if not REPORT_ID_RE.match(report_id):
        return None
    return REPORTS_DIR / f"{report_id}{suffix}"


def load_report(report_id: str) -> dict[str, Any] | None:
    p = _safe_path(report_id, ".json")
    if p is None:
        return None
    if not p.exists():
        # 本機已清掉（retention 汰換）→ 從 pCloud 冷儲存回補
        try:
            from publish.pcloud_backup import restore_report
            restore_report(report_id)
        except Exception:  # noqa: BLE001
            pass
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def load_markdown(report_id: str) -> str | None:
    p = _safe_path(report_id, ".md")
    if p is None or not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def latest_report_id() -> str | None:
    if not REPORTS_DIR.exists():
        return None
    files = sorted(REPORTS_DIR.glob("morning_*.json"))
    return files[-1].stem if files else None


def list_reports(limit: int = 120) -> list[dict[str, Any]]:
    """歷史報告清單（新到舊），給首頁列表用。只取輕量欄位。"""
    if not REPORTS_DIR.exists():
        return []
    files = sorted(REPORTS_DIR.glob("morning_*.json"), reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 壞檔跳過
            continue
        out.append({
            "report_id": d.get("report_id", f.stem),
            "report_type": d.get("report_type", "每日跨市場晨報"),
            "report_date": d.get("report_date"),
            "data_as_of": d.get("data_as_of"),
            "generated_at": d.get("generated_at"),
            "headline": d.get("headline", ""),
        })
    return out


def report_date_exists(d: date) -> bool:
    """當日（report_id 前綴 morning_YYYYMMDD）是否已有報告。給 scheduler catch-up 用。"""
    if not REPORTS_DIR.exists():
        return False
    return any(REPORTS_DIR.glob(f"morning_{d:%Y%m%d}_*.json"))
