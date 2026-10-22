"""決策供應商對照比較 CLI（spec 026 US4，SC-005）。"""
from __future__ import annotations

import json

import pytest

from reports import provider_compare as pc


def _clues(adjudicated: int, total: int):
    out = [{"outcome": "confirmed"}] * adjudicated
    return out + [{"outcome": "unchecked_budget"}] * (total - adjudicated)


def _main(day, *, decision=24.0, retrieval=1.0, adj=2, clues=5, errors=0, out=18_000,
          provider="anthropic", frugal=False):
    return {
        "report_id": f"morning_{day}_083000",
        "guardrail": {"error_count": errors},
        "cost": {"decision_provider": provider, "frugal_mode": frugal,
                 "decision_model": "claude-opus-5",
                 "decision_twd": decision, "retrieval_twd": retrieval,
                 "decision_output_tokens": out,
                 "tokens": {"output_tokens": out + 1_500},
                 "verification": {"clues": _clues(adj, clues), "fetch_requests": 3}},
    }


def _shadow(day, *, decision=11.0, adj=2, clues=5, errors=0, out=14_000, error=None):
    return {
        "report_id": f"morning_{day}_083000", "shadow_of": f"morning_{day}_083000",
        "error": error,
        "guardrail": {"error_count": errors},
        "cost": {"decision_provider": "openai", "frugal_mode": False,
                 "decision_model": "gpt-6.1-sol",
                 "decision_twd": decision, "retrieval_twd": 1.0,
                 "tokens": {"output_tokens": out, "reasoning_tokens": 8_000},
                 "verification": {"clues": _clues(adj, clues), "fetch_requests": 3}},
    }


def _write(root, day, main=None, shadow=None):
    root.mkdir(exist_ok=True)
    (root / "shadow").mkdir(exist_ok=True)
    if main is not None:
        (root / f"morning_{day}_083000.json").write_text(json.dumps(main), encoding="utf-8")
    if shadow is not None:
        (root / "shadow" / f"morning_{day}_083000_openai.json").write_text(
            json.dumps(shadow), encoding="utf-8")


def test_only_paired_days_compared(tmp_path):
    _write(tmp_path, "20261022", _main("20261022"), _shadow("20261022"))
    _write(tmp_path, "20261023", _main("20261023"))                   # 沒影子
    s = pc.collect(tmp_path)
    assert s["pairs"] == 1
    assert s["skipped"] == {"no_shadow": 1}
    assert s["first_day"] == s["last_day"] == "20261022"


def test_shadow_cost_uses_main_retrieval(tmp_path):
    _write(tmp_path, "20261022", _main("20261022", decision=24.0, retrieval=2.0),
           _shadow("20261022", decision=10.0))
    s = pc.collect(tmp_path)
    assert s["main"]["cost_mean"] == 26.0
    assert s["shadow"]["cost_mean"] == 12.0        # 10 + 主報告的 2.0


def test_failed_shadows_excluded_from_means(tmp_path):
    _write(tmp_path, "20261022", _main("20261022"), _shadow("20261022"))
    _write(tmp_path, "20261023", _main("20261023"),
           _shadow("20261023", decision=99.0, error="LLMUnavailable: 503"))
    s = pc.collect(tmp_path)
    assert s["pairs"] == 1 and s["shadow_failed"] == 1
    assert s["shadow"]["cost_mean"] == 12.0
    assert "LLMUnavailable" in pc.render(s)


def test_main_not_comparable_skipped(tmp_path):
    _write(tmp_path, "20261022", _main("20261022", frugal=True), _shadow("20261022"))
    _write(tmp_path, "20261023", _main("20261023", provider="gemini-fallback"),
           _shadow("20261023"))
    s = pc.collect(tmp_path)
    assert s["pairs"] == 0
    assert s["skipped"] == {"main_not_comparable": 2}


def test_all_gates_pass(tmp_path):
    for day in ("20261022", "20261023"):
        _write(tmp_path, day, _main(day), _shadow(day))
    s = pc.collect(tmp_path)
    g = s["gates"]
    assert g["cost"]["pass"] and g["cost"]["ratio"] == pytest.approx(12 / 25, abs=1e-4)
    assert g["adjudication"]["pass"] and g["adjudication"]["ratio"] == 1.0
    assert g["guardrail"]["pass"]
    assert g["recommend_switch"] is True
    assert "建議切換預設" in pc.render(s)


@pytest.mark.parametrize("shadow_kw,failing", [
    ({"decision": 20.0}, "cost"),                  # 21/25 = 84% > 60%
    ({"adj": 1}, "adjudication"),                  # 0.2/0.4 = 50% < 80%
    ({"errors": 2}, "guardrail"),
])
def test_each_gate_can_fail(tmp_path, shadow_kw, failing):
    _write(tmp_path, "20261022", _main("20261022"), _shadow("20261022", **shadow_kw))
    g = pc.collect(tmp_path)["gates"]
    assert g[failing]["pass"] is False
    assert g["recommend_switch"] is False


def test_main_zero_adjudication_does_not_divide_by_zero(tmp_path):
    _write(tmp_path, "20261022", _main("20261022", adj=0), _shadow("20261022", adj=0))
    g = pc.collect(tmp_path)["gates"]
    assert g["adjudication"]["ratio"] is None
    assert g["adjudication"]["pass"] is True


def test_output_tokens_use_decision_only_count(tmp_path):
    _write(tmp_path, "20261022", _main("20261022", out=18_000), _shadow("20261022", out=14_000))
    s = pc.collect(tmp_path)
    assert s["main"]["output_tokens_mean"] == 18_000     # 不含召回層的 1,500
    assert s["shadow"]["reasoning_tokens_mean"] == 8_000


def test_window_filter(tmp_path):
    for day in ("20261020", "20261022", "20261025"):
        _write(tmp_path, day, _main(day), _shadow(day))
    s = pc.collect(tmp_path, since="20261021", until="20261024")
    assert s["pairs"] == 1 and s["first_day"] == "20261022"


def test_cli_missing_dir_exits_nonzero(tmp_path):
    with pytest.raises(SystemExit) as ei:
        pc.main([str(tmp_path / "nope")])
    assert ei.value.code != 0


def test_cli_empty_dir_exits_nonzero_without_table(tmp_path, capsys):
    tmp_path.joinpath("shadow").mkdir()
    assert pc.main([str(tmp_path)]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert "沒有可比較的報告" in out.err


def test_cli_rejects_bad_date(tmp_path):
    with pytest.raises(SystemExit):
        pc.main([str(tmp_path), "--since", "2026-10-22"])


def test_cli_text_and_json(tmp_path, capsys):
    _write(tmp_path, "20261022", _main("20261022"), _shadow("20261022"))
    assert pc.main([str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "配對天數：1" in text and "gpt-6.1-sol" in text and "claude-opus-5" in text
    assert pc.main([str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pairs"] == 1 and data["gates"]["recommend_switch"] is True
