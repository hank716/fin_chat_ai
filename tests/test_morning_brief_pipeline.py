"""spec 006 測試基線：晨報管線端到端（LLM / 打分 / 回測皆替身），以及讀取端的路徑守衛。

真正跑的是 generate_morning_brief 本身、run_guardrails、report/markdown builders；
換掉的只有花錢或吃資料的部分（召回、決策、本地模型、行情）。
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from ai import retrieval
from ai.schemas import BriefResult, WatchItem
from cost import tracker as ct
from reports import morning_brief as mb
from reports import smallcap_sleeve, strategy_calibration

FEATS = {
    "as_of": "2026-10-14",
    "tw": {
        "stocks": {
            "2330": {"name": "台積電", "close": 900.0, "return_5d_pct": 3.0, "above_ma20": True},
            "2603": {"name": "長榮", "close": 150.0, "return_5d_pct": -8.0, "above_ma20": False},
            "1101": {"name": "台泥", "close": 30.0, "return_5d_pct": -5.0},
        },
        # 2330 沒有偏空訊號、9999 不在 stocks：兩者都不該被補進來
        "movers": {"top_losers_5d": [{"symbol": "2603"}, {"symbol": "2330"}, {"symbol": "1101"},
                                     {"symbol": "9999"}]},
    },
}


def _result():
    return BriefResult(
        headline="今日中性偏多",
        data_as_of=date(2026, 10, 14),
        tw_watchlist=[WatchItem(symbol="2330", name="台積電", thesis="技術面偏多")],
        tw_caution=[],
    )


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    calls: list[str] = []
    costs: list[tuple[float, str | None]] = []
    priced: list[tuple[str, bool]] = []

    monkeypatch.setattr(mb, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(mb, "_build_combined_features", lambda refresh: (json.loads(json.dumps(FEATS)), {"tw": 3}))
    monkeypatch.setattr(strategy_calibration, "build_calibration_block", lambda: "")
    monkeypatch.setattr(ct, "brief_should_degrade", lambda: False)
    monkeypatch.setattr(ct, "month_total", lambda: 100.0)
    monkeypatch.setattr(ct, "today_total", lambda: 10.0)
    monkeypatch.setattr(ct, "month_remaining", lambda: 700.0)
    monkeypatch.setattr(ct, "month_by_provider", lambda: {})
    monkeypatch.setattr(ct, "record_cost", lambda c, provider=None: costs.append((c, provider)))

    def cost_of_usage(usage, model, grounded=False):
        priced.append((model, grounded))
        return 0.5 if grounded else 2.0

    monkeypatch.setattr(ct, "cost_of_usage", cost_of_usage)
    monkeypatch.setattr(mb.gemini_client, "resolved_version", lambda m: None)

    def fetch_facts(feats, frugal=False):
        calls.append("facts")
        return retrieval.FactsPack(), {"input_tokens": 10}, mb.settings.gemini_model_qa

    def decide(feats, facts, calibration, frugal=False):
        calls.append("decide")
        return mb.Decision(_result(), {"input_tokens": 20, "output_tokens": 5}, "anthropic", [], [])

    monkeypatch.setattr(mb, "_fetch_facts", fetch_facts)
    monkeypatch.setattr(mb, "_decide_brief", decide)
    monkeypatch.setattr(mb, "_verification_stats", lambda *a, **k: ({"active": True}, []))

    real_guard = mb.run_guardrails

    def guard(result, feats, **kw):
        calls.append("guardrail")
        return real_guard(result, feats, **kw)

    monkeypatch.setattr(mb, "run_guardrails", guard)
    monkeypatch.setattr(strategy_calibration, "score_risk", lambda cands: {"2330": 0.3})
    monkeypatch.setattr(strategy_calibration, "score_meta", lambda cands: {})
    monkeypatch.setattr(strategy_calibration, "fuse_scores", lambda cands: ({}, {}, {}))
    monkeypatch.setattr(strategy_calibration, "sizing_plan", lambda: None)
    monkeypatch.setattr(strategy_calibration, "market_exposure_scalar", lambda: (1.0, {}))
    monkeypatch.setattr(smallcap_sleeve, "build", lambda tw: {})
    monkeypatch.setattr(mb, "_run_backtest_loop", lambda: {})
    return {"calls": calls, "costs": costs, "priced": priced, "dir": tmp_path / "reports"}


# ── T014 依序 召回→決策→guardrail→落地；report 含必要欄位 ──

def test_brief_runs_in_order_and_lands_json_and_md(pipeline):
    report = mb.generate_morning_brief()
    assert pipeline["calls"] == ["facts", "decide", "guardrail"]
    rid = report["report_id"]
    assert mb.REPORT_ID_RE.match(rid)
    for key in ("headline", "tw_watchlist", "markdown", "copy_for_ai", "features", "report_date"):
        assert key in report, key
    assert (pipeline["dir"] / f"{rid}.json").exists() and (pipeline["dir"] / f"{rid}.md").exists()
    assert mb.load_report(rid)["report_id"] == rid
    assert mb.latest_report_id() == rid


# ── T015 load_report 非法 report_id → None（路徑穿越）──

@pytest.mark.parametrize("bad", ["../secrets", "morning_20261014_082000/../../x", "morning_2026",
                                 "morning_20261014_082000.json", "", "MORNING_20261014_082000"])
def test_illegal_report_ids_are_rejected(bad, tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "REPORTS_DIR", tmp_path)
    assert mb.load_report(bad) is None
    assert mb.load_markdown(bad) is None


def test_missing_report_tries_cold_restore_then_none(tmp_path, monkeypatch):
    from publish import pcloud_backup

    tried = []
    monkeypatch.setattr(mb, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(pcloud_backup, "restore_report", lambda rid: tried.append(rid))
    assert mb.load_report("morning_20260101_082000") is None
    assert tried == ["morning_20260101_082000"]


# ── T016 打分函式拋例外 → 晨報仍完成、缺該 *_scores ──

def test_scoring_failures_do_not_stop_the_brief(pipeline, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("model file corrupt")

    for name in ("score_risk", "score_meta", "fuse_scores", "sizing_plan"):
        monkeypatch.setattr(strategy_calibration, name, boom)
    monkeypatch.setattr(smallcap_sleeve, "build", boom)
    report = mb.generate_morning_brief()
    for key in ("risk_scores", "meta_scores", "fused_scores", "size_weights", "tw_smallcap"):
        assert key not in report, key
    assert (pipeline["dir"] / f"{report['report_id']}.json").exists()


def test_successful_scoring_is_recorded(pipeline):
    report = mb.generate_morning_brief()
    assert report["risk_scores"] == {"2330": 0.3}


# ── T017（依 spec 022 改寫）召回 grounded 記 gemini、決策非 grounded 記 anthropic ──

def test_costs_split_by_layer_and_provider(pipeline):
    report = mb.generate_morning_brief()
    s = mb.settings
    assert pipeline["priced"] == [(s.gemini_model_qa, True), (s.claude_model_decision, False)]
    assert pipeline["costs"] == [(0.5, "gemini"), (2.0, "anthropic")]
    assert report["cost"]["brief_twd"] == 2.5
    assert (report["cost"]["retrieval_twd"], report["cost"]["decision_twd"]) == (0.5, 2.0)


# ── T018 report 含 guardrail 與 cost 區塊 ──

def test_report_carries_guardrail_and_cost_blocks(pipeline):
    report = mb.generate_morning_brief()
    assert {"passed", "error_count", "warning_count"} <= set(report["guardrail"])
    for key in ("brief_twd", "month_total_twd", "monthly_limit_twd", "month_remaining_twd",
                "decision_provider", "frugal_mode", "verification"):
        assert key in report["cost"], key
    assert report["decision_provider"] == "anthropic"


# ── T019 _backfill_caution：以 movers 真實數據補齊、不硬湊 ──

def test_backfill_caution_fills_from_movers_with_real_signals():
    result = _result()
    added = mb._backfill_caution(result, FEATS, want=5)
    syms = [w.symbol for w in result.tw_caution]
    assert syms == ["2603", "1101"] and added == 2          # 沒有可佐證的偏空訊號就不硬湊
    item = result.tw_caution[0]
    assert item.name == "長榮" and item.signals == ["近5日-8.0%", "跌破MA20"]
    assert "自動補列" in item.thesis


def test_backfill_caution_is_noop_when_list_is_full():
    result = _result()
    result.tw_caution = [WatchItem(symbol=s, name=s, thesis="x") for s in "ABCDE"]
    assert mb._backfill_caution(result, FEATS, want=5) == 0


def test_backfill_caution_skips_symbols_already_listed():
    result = _result()
    result.tw_caution = [WatchItem(symbol="2603", name="長榮", thesis="模型自己列的")]
    mb._backfill_caution(result, FEATS, want=5)
    assert [w.symbol for w in result.tw_caution] == ["2603", "1101"]
    assert result.tw_caution[0].thesis == "模型自己列的"


# ── spec 026：OpenAI 決策路徑 ──────────────────────────────────────────────

def _openai_decision(**kw):
    return mb.Decision(_result(), {"input_tokens": 20, "output_tokens": 5,
                                   "reasoning_tokens": 3}, "openai", [], [], **kw)


def test_openai_decision_priced_and_recorded_as_openai(pipeline, monkeypatch):
    monkeypatch.setattr(mb, "_decide_brief",
                        lambda *a, **k: _openai_decision(model_version="gpt-6.1-sol-2026-10-01"))
    report = mb.generate_morning_brief()
    s = mb.settings
    assert pipeline["priced"][-1] == (s.openai_model_decision, False)
    assert pipeline["costs"][-1] == (2.0, "openai")
    cost = report["cost"]
    assert report["decision_provider"] == cost["decision_provider"] == "openai"
    assert cost["decision_model"] == s.openai_model_decision
    assert cost["decision_model_version"] == "gpt-6.1-sol-2026-10-01"
    assert cost["tokens"]["reasoning_tokens"] == 3
    assert "decision_fallback_reason" not in cost


def test_default_anthropic_report_shape_unchanged_except_new_fields(pipeline):
    report = mb.generate_morning_brief()
    cost = report["cost"]
    assert cost["decision_model"] == mb.settings.claude_model_decision
    assert cost["decision_model_version"] is None
    assert cost["tokens"]["reasoning_tokens"] == 0


@pytest.mark.parametrize("provider,frugal,active", [
    ("openai", False, True), ("openai", True, False),
    ("anthropic", False, True), ("gemini-fallback", False, False),
])
def test_verification_active_by_provider(pipeline, monkeypatch, provider, frugal, active):
    seen = {}

    def stats(*a, **k):
        seen.update(k)
        return {"active": k["active"]}, []

    monkeypatch.setattr(mb, "_verification_stats", stats)
    monkeypatch.setattr(ct, "brief_should_degrade", lambda: frugal)
    monkeypatch.setattr(mb, "_decide_brief", lambda *a, **k: mb.Decision(
        _result(), {}, provider, [], []))
    mb.generate_morning_brief()
    assert seen["active"] is active
    assert seen["provider"] == provider


def test_fetch_limit_follows_provider(monkeypatch):
    monkeypatch.setattr(mb.settings, "claude_brief_fetch_uses", 3)
    monkeypatch.setattr(mb.settings, "openai_brief_fetch_uses", 5)
    stats, _ = mb._verification_stats(retrieval.FactsPack(), [], {}, [], active=True,
                                      provider="openai")
    assert stats["fetch_limit"] == 5
    stats, _ = mb._verification_stats(retrieval.FactsPack(), [], {}, [], active=True)
    assert stats["fetch_limit"] == 3


def test_fallback_reason_visible_in_markdown(pipeline, monkeypatch):
    monkeypatch.setattr(mb, "_decide_brief", lambda *a, **k: mb.Decision(
        _result(), {}, "gemini-fallback", [], [],
        fallback_reason="openai: OPENAI_API_KEY 未設定"))
    report = mb.generate_morning_brief()
    assert report["cost"]["decision_fallback_reason"] == "openai: OPENAI_API_KEY 未設定"
    assert "OPENAI_API_KEY 未設定" in report["markdown"]


# ── _decide_brief + 真的 OpenAIDecisionLLM（只換掉 SDK client）──

def _facts_pack(url="https://news.example.com/a?id=1"):
    return retrieval.FactsPack(events=[retrieval.FactEvent(
        claim="台積電法說會上修財測", date="2026-10-21", source="鉅亨", url=url)])


def _fake_openai(monkeypatch, responses):
    from types import SimpleNamespace

    from ai import openai_client as oc

    queue = list(responses)

    def create(**kw):
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(oc, "_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(create=create)))


def _gemini_fallback(monkeypatch):
    from ai import llm_client
    from ai.schemas import BriefDraft

    draft = BriefDraft(headline="降級產出", sections=[], tw_watchlist=[], tw_caution=[],
                       risks=[], follow_ups=[], news_digest=[], fact_checks=[],
                       data_as_of="2026-10-22", sources=[])
    monkeypatch.setattr(llm_client.GeminiDecisionLLM, "draft_brief",
                        lambda self, *a, **k: (draft, {"input_tokens": 1}, []))


def _oa_resp(output, status="completed"):
    from types import SimpleNamespace
    usage = SimpleNamespace(input_tokens=100, output_tokens=10,
                            input_tokens_details=SimpleNamespace(cached_tokens=0,
                                                                 cache_write_tokens=0),
                            output_tokens_details=SimpleNamespace(reasoning_tokens=0))
    return SimpleNamespace(output=output, usage=usage, status=status, model="gpt-6.1-sol-x",
                           incomplete_details=None, error=None)


def _oa_fetch_call(url):
    from types import SimpleNamespace
    return SimpleNamespace(type="function_call", name="web_fetch", call_id="c1", id="fc1",
                           arguments=json.dumps({"url": url}))


def _oa_message(text):
    from types import SimpleNamespace
    return SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text",
                                                                    text=text)])


@pytest.fixture
def openai_provider(monkeypatch):
    import socket

    import httpx

    from ai import fetch_tool

    monkeypatch.setattr(mb.settings, "llm_decision_provider", "openai")
    monkeypatch.setattr(mb.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(fetch_tool.socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                      ("93.184.216.34", port))])
    body = ("<html><body><p>" + "台積電上修財測" * 50 + "</p></body></html>").encode()
    real_client = httpx.Client

    def client_with_mock(*a, **k):
        k["transport"] = httpx.MockTransport(
            lambda req: httpx.Response(200, headers={"content-type": "text/html"}, content=body))
        return real_client(*a, **k)

    monkeypatch.setattr(fetch_tool.httpx, "Client", client_with_mock)


def test_decide_brief_openai_success(monkeypatch, openai_provider):
    url = "https://news.example.com/a?id=1"
    draft = {
        "headline": "OpenAI 晨報", "sections": [], "tw_watchlist": [], "tw_caution": [],
        "risks": [], "follow_ups": [], "news_digest": [],
        "fact_checks": [{"url": url, "verdict": "confirmed", "note": "原文一致"}],
        "data_as_of": "2026-10-22", "sources": [],
    }
    _fake_openai(monkeypatch, [_oa_resp([_oa_fetch_call(url)]),
                               _oa_resp([_oa_message(json.dumps(draft, ensure_ascii=False))])])
    decision = mb._decide_brief({}, _facts_pack(url), "")
    assert decision.provider == "openai"
    assert decision.result.headline == "OpenAI 晨報"
    assert decision.model_version == "gpt-6.1-sol-x"
    assert decision.fact_checks[0]["verdict"] == "confirmed"
    assert len(decision.fetch_attempts) == 1 and decision.fetch_attempts[0].ok
    assert decision.usage["rounds"] == 2


def test_decide_brief_openai_failure_falls_back_keeping_attempts(monkeypatch, openai_provider):
    import httpx
    import openai

    url = "https://news.example.com/a?id=1"
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    _fake_openai(monkeypatch, [_oa_resp([_oa_fetch_call(url)]),
                               openai.APIConnectionError(request=req)])
    _gemini_fallback(monkeypatch)
    decision = mb._decide_brief({}, _facts_pack(url), "")
    assert decision.provider == "gemini-fallback"
    assert decision.result.headline == "降級產出"
    assert len(decision.fetch_attempts) == 1          # 倒下前那次查證仍在
    assert decision.fallback_reason.startswith("openai:")


def test_decide_brief_openai_without_key_falls_back_with_reason(monkeypatch, openai_provider):
    from ai import openai_client as oc

    oc.reset_client()
    monkeypatch.setattr(mb.settings, "openai_api_key", "")
    _gemini_fallback(monkeypatch)
    decision = mb._decide_brief({}, _facts_pack(), "")
    assert decision.provider == "gemini-fallback"
    assert "OPENAI_API_KEY 未設定" in decision.fallback_reason
