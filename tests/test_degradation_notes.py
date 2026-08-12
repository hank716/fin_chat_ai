"""降級提示（憲章 II：降級 MUST 對使用者可見）。

2026-08-12 盤點發現：`frugal_mode` 與 `decision_provider=gemini-fallback` 只存在於報告 JSON
的 cost 區塊與一行 log，markdown / Discord / 網頁三個使用者實際閱讀的面上完全看不到。
月底連續一週拿到沒有外部事件的晨報，讀的人會以為「今天真的沒事發生」。

這裡釘住的是「哪些情況必須出現提示」與「哪些情況不該誤報」。
"""
from __future__ import annotations

from reports.degradation import degradation_notes


def test_healthy_report_has_no_notes():
    """常態晨報不該掛任何警告——狼來了會讓真正的降級被忽略。"""
    cost = {
        "frugal_mode": False,
        "decision_provider": "anthropic",
        "verification": {"facts_n": 8, "fact_checks_n": 8, "unadjudicated_n": 0,
                         "fetch_requests": 5},
    }
    assert degradation_notes(cost) == []


def test_missing_cost_block_is_tolerated():
    """舊報告沒有 cost/verification 欄位，不該炸掉整個 render。"""
    assert degradation_notes(None) == []
    assert degradation_notes({}) == []
    assert degradation_notes({"frugal_mode": False}) == []


def test_frugal_mode_is_surfaced():
    notes = degradation_notes({"frugal_mode": True, "verification": {}})
    assert any("節儉模式" in n for n in notes)


def test_provider_fallback_is_surfaced():
    notes = degradation_notes({"decision_provider": "gemini-fallback", "verification": {}})
    assert any("gemini-fallback" in n and "未經查證" in n for n in notes)


def test_idle_verification_layer_is_surfaced():
    """有線索、工具也掛了，但 web_fetch 一次都沒開＝verdict 沒有原文佐證。"""
    cost = {
        "frugal_mode": False,
        "verification": {"facts_n": 8, "fact_checks_n": 8, "fetch_requests": 0},
    }
    assert any("未實際開啟任何來源" in n for n in degradation_notes(cost))


def test_frugal_mode_does_not_double_report_idle_verification():
    """節儉模式本來就沒掛工具，0 次 fetch 是預期行為，不該再報一次『空轉』。"""
    cost = {
        "frugal_mode": True,
        "verification": {"facts_n": 0, "fact_checks_n": 0, "fetch_requests": 0},
    }
    notes = degradation_notes(cost)
    assert len(notes) == 1 and "節儉模式" in notes[0]


def test_unadjudicated_clues_are_surfaced():
    """8/10、8/11 實際發生過：8 則線索只回 6 條 fact_checks，兩則被靜默略過。"""
    cost = {
        "verification": {"facts_n": 8, "fact_checks_n": 6, "unadjudicated_n": 2,
                         "fetch_requests": 3},
    }
    notes = degradation_notes(cost)
    assert any("2 則外部線索未被裁決" in n for n in notes)
