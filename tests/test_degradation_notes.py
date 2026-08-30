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


# ── spec 023 US2：查證失敗的分流提示 ─────────────────────────────────────

def _cost(outcomes, **extra):
    verification = {"facts_n": sum(outcomes.values()), "fact_checks_n": sum(outcomes.values()),
                    "fetch_requests": 3, "fetch_limit": 3, "outcomes": outcomes,
                    "checked_n": sum(v for k, v in outcomes.items()
                                     if k in ("confirmed", "contradicted", "checked_insufficient"))}
    verification.update(extra)
    return {"frugal_mode": False, "decision_provider": "anthropic",
            "verification": verification}


def test_budget_shortfall_says_so_explicitly():
    """「額度不足」與「來源打不開」要修的東西不同，講成同一句話等於兩邊都修不了。"""
    notes = degradation_notes(_cost({"confirmed": 2, "unchecked_budget": 3}))
    assert any("查證額度不足" in n and "3 則" in n for n in notes)
    assert not any("來源無法開啟" in n for n in notes)


def test_unreachable_sources_say_so_explicitly():
    notes = degradation_notes(_cost({"confirmed": 2, "unchecked_unreachable": 2}))
    assert any("來源無法開啟" in n and "2 則" in n for n in notes)
    assert not any("查證額度不足" in n for n in notes)


def test_mixed_failures_are_both_reported():
    notes = degradation_notes(_cost(
        {"confirmed": 1, "unchecked_budget": 2, "unchecked_unreachable": 1}))
    assert any("查證額度不足" in n for n in notes)
    assert any("來源無法開啟" in n for n in notes)


def test_all_unchecked_reads_as_no_verified_events():
    """FR-008：全數未查證時，呈現效果要等同「本篇無經查證外部事件」。

    否則讀者會把「本篇有 5 則外部事件」當成那 5 則有事實基礎。
    """
    notes = degradation_notes(_cost({"unchecked_budget": 3, "unchecked_unreachable": 2}))
    assert any("本篇無經查證的外部事件" in n for n in notes)


def test_fully_checked_report_stays_quiet():
    """全部查證成功就不該有任何提示——狼來了會讓真正的降級被忽略。"""
    assert degradation_notes(_cost({"confirmed": 3, "checked_insufficient": 1})) == []


def test_claimed_but_unbacked_verdicts_are_surfaced():
    """模型說已查證、工具卻沒有成功紀錄——這是最該讓讀者看到的一類（8/20 實際發生）。"""
    notes = degradation_notes(_cost({"confirmed": 1, "unchecked_budget": 1},
                                    claimed_unbacked_n=1))
    assert any("無實際開啟來源的紀錄" in n for n in notes)


def test_frugal_mode_does_not_report_verification_failures():
    """節儉模式本來就不掛查證工具，再報一次「查證失敗」是雜訊。"""
    notes = degradation_notes({"frugal_mode": True,
                               "verification": {"outcomes": {"unchecked_other": 3}}})
    assert len(notes) == 1 and "節儉模式" in notes[0]


def test_notes_carry_no_markdown_syntax():
    """網頁面是 `{{ note }}` 逐字跳脫輸出——文案帶 markdown 就會變成一排星號。"""
    notes = degradation_notes(_cost({"unchecked_budget": 2, "unchecked_unreachable": 1},
                                    claimed_unbacked_n=1))
    assert notes and all("*" not in n for n in notes)


def test_fallback_provider_does_not_stack_verification_notes():
    """降級到 Gemini 兩段式時**沒有查證層可言**，不是「查證全失敗」。

    把它當查證失敗會疊出四句意思重疊的提示（「未經查證」講三遍），真正的訊息反而被稀釋。
    降級本身已經有專屬的一句話了。
    """
    cost = {"frugal_mode": False, "decision_provider": "gemini-fallback",
            "verification": {"facts_n": 2, "fact_checks_n": 0, "unadjudicated_n": 2,
                             "fetch_requests": 0, "fetch_limit": 6,
                             "verification_active": False,
                             "outcomes": {"unchecked_other": 2}, "checked_n": 0}}
    notes = degradation_notes(cost)
    assert len(notes) == 1 and "gemini-fallback" in notes[0]


def test_legacy_fallback_report_without_active_flag_is_also_quiet():
    """`verification_active` 是 spec 023 之後才有的欄位；舊的降級報告同樣不該被疊加。"""
    cost = {"decision_provider": "gemini-fallback",
            "verification": {"facts_n": 5, "fact_checks_n": 0, "unadjudicated_n": 5,
                             "fetch_requests": 0}}
    notes = degradation_notes(cost)
    assert len(notes) == 1 and "gemini-fallback" in notes[0]


def test_remaining_clues_are_reported_alongside_known_causes():
    """8 則線索只交代 6 則、剩 2 則憑空消失＝US2 想解決的問題本身。

    「已經報了額度不足」不是省略其他未查證線索的理由——每一則都要有去向。
    """
    notes = degradation_notes(_cost({
        "confirmed": 3, "unchecked_budget": 2,
        "unchecked_unreachable": 1, "unchecked_transient": 2,
    }))
    assert any("查證額度不足" in n for n in notes)
    assert any("來源無法開啟" in n for n in notes)
    assert any("2 則外部線索未經查證" in n for n in notes)

