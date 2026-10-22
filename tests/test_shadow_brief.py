"""同日影子對照（spec 026 US4）。

三條硬規則：只存檔不發布（SC-007）、不影響主晨報、不把之後的主晨報推進節儉模式（FR-017a）。
"""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta

import pytest

from ai import llm_client, retrieval
from ai.errors import LLMUnavailable
from ai.fetch_tool import FetchAttempt
from ai.schemas import BriefDraft, BriefResult, WatchItem
from cost import tracker as ct
from reports import morning_brief as mb
from reports import shadow_brief, smallcap_sleeve, strategy_calibration

FEATS = {"as_of": "2026-10-22", "tw": {"stocks": {
    "2330": {"name": "台積電", "close": 900.0, "return_5d_pct": 3.0, "above_ma20": True}}}}
URL = "https://news.example.com/a?id=1"
FACTS = retrieval.FactsPack(events=[retrieval.FactEvent(
    claim="台積電上修財測", date="2026-10-21", source="鉅亨", url=URL)])


def _result():
    return BriefResult(headline="主晨報", data_as_of=date(2026, 10, 22),
                       tw_watchlist=[WatchItem(symbol="2330", name="台積電", thesis="t")])


def _draft():
    return BriefDraft(
        headline="影子晨報", sections=[], tw_watchlist=[], tw_caution=[], risks=[],
        follow_ups=[], news_digest=[], data_as_of="2026-10-22", sources=[],
        fact_checks=[{"url": URL, "verdict": "confirmed", "note": "一致"}],
    )


class _ShadowLLM:
    name = "openai"

    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []
        self.last_model_version = "gpt-6.1-sol-2026-10-01"

    def draft_brief(self, feats, facts_json, calibration, *, frugal=False):
        self.calls.append((feats, facts_json, calibration, frugal))
        if self.exc:
            raise self.exc
        attempt = FetchAttempt(url=URL, ok=True, error_code=None,
                               retrieved_at="2026-10-22T00:30:00+00:00", content_chars=5000)
        return _draft(), {"input_tokens": 60_000, "output_tokens": 9_000,
                          "reasoning_tokens": 5_000, "web_fetch_requests": 1}, [attempt]


@pytest.fixture
def env(tmp_path, monkeypatch):
    events: list = []
    costs: list = []
    remaining = {"v": 700.0}
    shadow_llm = _ShadowLLM()

    monkeypatch.setattr(mb, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(mb.settings, "openai_shadow_until", date.today() + timedelta(days=5))
    monkeypatch.setattr(mb.settings, "brief_degrade_reserve_twd", 45)
    monkeypatch.setattr(mb.settings, "openai_shadow_cost_estimate_twd", 15.0)
    monkeypatch.setattr(mb, "_build_combined_features",
                        lambda refresh: (copy.deepcopy(FEATS), {"tw": 1}))
    monkeypatch.setattr(strategy_calibration, "build_calibration_block", lambda: "校準")
    monkeypatch.setattr(ct, "brief_should_degrade", lambda: False)
    monkeypatch.setattr(ct, "month_total", lambda: 100.0)
    monkeypatch.setattr(ct, "today_total", lambda: 10.0)
    monkeypatch.setattr(ct, "month_remaining", lambda: remaining["v"])
    monkeypatch.setattr(ct, "month_by_provider", lambda: {})
    monkeypatch.setattr(ct, "record_cost", lambda c, provider=None: costs.append((c, provider)))
    monkeypatch.setattr(ct, "cost_of_usage", lambda usage, model, grounded=False:
                        0.5 if grounded else (12.0 if model.startswith("gpt") else 26.0))
    monkeypatch.setattr(mb.gemini_client, "resolved_version", lambda m: None)

    def fetch_facts(feats, frugal=False):
        events.append("facts")
        return FACTS, {"input_tokens": 10}, mb.settings.gemini_model_qa

    monkeypatch.setattr(mb, "_fetch_facts", fetch_facts)
    monkeypatch.setattr(mb, "_decide_brief", lambda *a, **k: mb.Decision(
        _result(), {"input_tokens": 20, "output_tokens": 5}, "anthropic", [], []))
    monkeypatch.setattr(strategy_calibration, "score_risk", lambda c: {})
    monkeypatch.setattr(strategy_calibration, "score_meta", lambda c: {})
    monkeypatch.setattr(strategy_calibration, "fuse_scores", lambda c: ({}, {}, {}))
    monkeypatch.setattr(strategy_calibration, "sizing_plan", lambda: None)
    monkeypatch.setattr(strategy_calibration, "market_exposure_scalar", lambda: (1.0, {}))
    monkeypatch.setattr(smallcap_sleeve, "build", lambda tw: {})
    monkeypatch.setattr(mb, "_run_backtest_loop", lambda: {})
    monkeypatch.setattr(llm_client, "get_decision_llm",
                        lambda provider=None: shadow_llm if provider == "openai" else None)

    # 發布管道：記下呼叫順序與當下送出的內容
    from notify import discord
    from publish import pcloud_backup, supabase_publish
    from storage import retention

    def send(report):
        events.append(("discord", copy.deepcopy(report)))
        return True

    monkeypatch.setattr(discord, "send_daily_summary", send)
    monkeypatch.setattr(pcloud_backup, "backup_report", lambda j, m: events.append("pcloud") or {})
    monkeypatch.setattr(supabase_publish, "publish_report_index",
                        lambda report, **k: events.append(("supabase", copy.deepcopy(report))))
    monkeypatch.setattr(retention, "enforce_retention", lambda: events.append("retention"))

    real_run = shadow_brief.run_shadow

    def run_shadow(*a, **k):
        events.append("shadow")
        return real_run(*a, **k)

    monkeypatch.setattr(shadow_brief, "run_shadow", run_shadow)
    return {"events": events, "costs": costs, "remaining": remaining, "llm": shadow_llm,
            "dir": tmp_path / "reports"}


def _names(events):
    return [e if isinstance(e, str) else e[0] for e in events]


def test_shadow_runs_after_publish_and_writes_subdir(env):
    report = mb.generate_morning_brief(push_discord=True, publish=True)
    names = _names(env["events"])
    assert names.index("shadow") > names.index("discord")
    assert names.index("shadow") > names.index("supabase")
    assert names.index("shadow") > names.index("retention")

    path = env["dir"] / "shadow" / f"{report['report_id']}_openai.json"
    assert path.exists()
    shadow = json.loads(path.read_text(encoding="utf-8"))
    assert shadow["shadow_of"] == report["report_id"]
    assert shadow["decision_provider"] == "openai"
    assert shadow["decision_model_version"] == "gpt-6.1-sol-2026-10-01"
    assert shadow["error"] is None
    assert shadow["result"]["headline"] == "影子晨報"
    assert shadow["fact_checks"][0]["verdict"] == "confirmed"
    assert shadow["cost"]["decision_twd"] == 12.0
    assert shadow["cost"]["retrieval_twd"] == 0.5        # 共用主報告的召回成本
    assert shadow["cost"]["verification"]["clues"][0]["outcome"] == "confirmed"
    assert {"passed", "error_count"} <= set(shadow["guardrail"])
    assert shadow["markdown"]

    assert report["shadow"] == {"status": "ok", "reason": None, "path": str(path)}
    on_disk = json.loads((env["dir"] / f"{report['report_id']}.json").read_text(encoding="utf-8"))
    assert on_disk["shadow"]["status"] == "ok"


def test_shadow_reuses_same_features_and_facts(env):
    mb.generate_morning_brief()
    assert env["events"].count("facts") == 1              # 召回層只跑一次
    feats, facts_json, calibration, frugal = env["llm"].calls[0]
    assert facts_json == FACTS.to_prompt_json()
    assert calibration == "校準"
    assert feats["as_of"] == FEATS["as_of"]
    assert frugal is False


def test_shadow_invisible_to_report_listing(env):
    report = mb.generate_morning_brief()
    assert mb.latest_report_id() == report["report_id"]
    assert [r["report_id"] for r in mb.list_reports()] == [report["report_id"]]
    assert mb.report_date_exists(date.today())
    assert len(list(env["dir"].glob("morning_*.json"))) == 1


def test_publish_channels_never_carry_shadow_content(env):
    mb.generate_morning_brief(push_discord=True, publish=True)
    for event in env["events"]:
        if isinstance(event, tuple):
            dumped = json.dumps(event[1], ensure_ascii=False)
            assert "影子晨報" not in dumped
            assert "shadow" not in event[1]


def test_shadow_cost_recorded_to_openai_bucket(env):
    mb.generate_morning_brief()
    assert (12.0, "openai") in env["costs"]
    assert (26.0, "anthropic") in env["costs"]


def test_budget_reserve_skips_shadow(env):
    env["remaining"]["v"] = 55.0          # 55 - 15 < 45
    report = mb.generate_morning_brief()
    assert report["shadow"] == {"status": "skipped", "reason": "budget", "path": None}
    assert "shadow" not in env["events"]
    assert not (env["dir"] / "shadow").exists()
    assert all(p != "openai" for _c, p in env["costs"])


def test_shadow_exception_does_not_affect_main(env):
    env["llm"].exc = LLMUnavailable("OpenAI 503")
    env["llm"].exc.usage = {"input_tokens": 1000, "output_tokens": 10}
    env["llm"].exc.fetch_attempts = [FetchAttempt(url=URL, ok=False,
                                                  error_code="url_not_accessible",
                                                  retrieved_at=None)]
    report = mb.generate_morning_brief()
    assert report["headline"] == "主晨報"
    assert report["shadow"]["status"] == "error"
    shadow = json.loads((env["dir"] / "shadow" / f"{report['report_id']}_openai.json")
                        .read_text(encoding="utf-8"))
    assert "OpenAI 503" in shadow["error"]
    assert shadow["cost"]["verification"]["fetch_failed"] == 1
    assert (12.0, "openai") in env["costs"]        # 失敗前已花的錢照實記帳


def test_unexpected_shadow_crash_is_contained(env, monkeypatch):
    """非 LLMError 的意外（例如 guardrail 自己炸了）也只能停在影子裡。

    主晨報用的是 `mb.run_guardrails`（匯入時綁定的名稱），影子在呼叫時才從
    `guardrails.verify` 取——只弄壞後者，主路徑不受影響。
    """
    from guardrails import verify

    def boom(*a, **k):
        raise RuntimeError("guardrail 壞了")

    monkeypatch.setattr(verify, "run_guardrails", boom)
    report = mb.generate_morning_brief()
    assert report["headline"] == "主晨報"
    assert report["shadow"]["status"] == "error"
    assert report["shadow"]["reason"] == "RuntimeError"


@pytest.mark.parametrize("until,reason", [
    (None, "disabled"),
    (date.today() - timedelta(days=1), "expired"),
])
def test_shadow_off_or_expired(env, monkeypatch, until, reason):
    monkeypatch.setattr(mb.settings, "openai_shadow_until", until)
    report = mb.generate_morning_brief()
    assert "shadow" not in env["events"]
    if reason == "disabled":
        assert "shadow" not in report          # 預設關閉：主報告逐欄不變
    else:
        assert report["shadow"]["reason"] == "expired"


def test_shadow_runs_on_last_day(env, monkeypatch):
    monkeypatch.setattr(mb.settings, "openai_shadow_until", date.today())
    report = mb.generate_morning_brief()
    assert report["shadow"]["status"] == "ok"


def test_frugal_main_skips_shadow(env, monkeypatch):
    monkeypatch.setattr(ct, "brief_should_degrade", lambda: True)
    report = mb.generate_morning_brief()
    assert report["shadow"]["reason"] == "frugal"
    assert "shadow" not in env["events"]


@pytest.mark.parametrize("provider", ["openai", "gemini-fallback"])
def test_no_control_group_skips_shadow(env, monkeypatch, provider):
    monkeypatch.setattr(mb, "_decide_brief", lambda *a, **k: mb.Decision(
        _result(), {}, provider, [], []))
    report = mb.generate_morning_brief()
    assert report["shadow"]["status"] == "skipped"
    assert "shadow" not in env["events"]


def test_verification_stats_cli_reads_shadow_dir(env):
    from reports import verification_stats as vs

    mb.generate_morning_brief()
    summary = vs.aggregate(env["dir"] / "shadow")
    assert summary["days"] == 1
    assert summary["outcomes"] == {"confirmed": 1}
