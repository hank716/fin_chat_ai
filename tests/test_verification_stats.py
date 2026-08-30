"""查證結局分類與彙總（spec 023-verification-efficacy，US1）。

這裡釘住的是「分錯了不會有任何錯誤訊息、只會讓調參依據整個歪掉」的性質：

(a) 「額度不足所以沒查」與「查了但原文不足」必須分屬不同類——混在一起就是 spec 023
    存在的理由（8/10–8/12 只能靠讀中文 note 猜是哪一種）。
(b) 模型說 confirmed、工具卻沒有成功紀錄時，不得算成已核對（FR-006）。
    8/20 實際發生過：4 則 confirmed 分屬 4 個 URL，當天只開了 3 次。
(c) 舊報告缺欄位不得炸、也不得混進新格式的分母。
"""
from __future__ import annotations

import json

from reports import verification_stats as vs


def _attempt(url, ok=True, error_code=None):
    return {"url": url, "ok": ok, "error_code": error_code}


def _check(url, verdict):
    return {"url": url, "verdict": verdict, "note": "n/a"}


def _one(clues, fact_checks, attempts, *, limit=3, used=None):
    outcomes = vs.classify_outcomes(
        clues, fact_checks, attempts,
        fetch_limit=limit, fetch_requests=len(attempts) if used is None else used,
    )
    assert len(outcomes) == 1
    return outcomes[0]


# ── 七類分類（FR-001 / FR-002）──────────────────────────────────────────

def test_confirmed_requires_a_successful_fetch():
    out = _one(["https://a.example/1"], [_check("https://a.example/1", "confirmed")],
               [_attempt("https://a.example/1")])
    assert out.outcome == vs.CONFIRMED
    assert out.claimed_unbacked is False


def test_contradicted_counts_as_checked():
    """contradicted 是**有效的查證結果**——實際開過原文才知道不符。"""
    out = _one(["https://a.example/1"], [_check("https://a.example/1", "contradicted")],
               [_attempt("https://a.example/1")])
    assert out.outcome == vs.CONTRADICTED
    assert out.outcome in vs.CHECKED


def test_opened_but_insufficient_is_not_unchecked():
    """開了原文卻判 unverifiable ≠ 沒查。這一格從 unverifiable 分出來是本 spec 的重點。"""
    out = _one(["https://a.example/1"], [_check("https://a.example/1", "unverifiable")],
               [_attempt("https://a.example/1")])
    assert out.outcome == vs.CHECKED_INSUFFICIENT
    assert out.outcome in vs.CHECKED


def test_max_uses_exceeded_is_budget():
    out = _one(["https://a.example/1"], [_check("https://a.example/1", "unverifiable")],
               [_attempt("https://a.example/1", ok=False, error_code="max_uses_exceeded")])
    assert out.outcome == vs.UNCHECKED_BUDGET


def test_unreachable_codes_are_source_problems():
    for code in ("url_not_accessible", "url_not_allowed",
                 "unsupported_content_type", "url_too_long"):
        out = _one(["https://a.example/1"], [],
                   [_attempt("https://a.example/1", ok=False, error_code=code)])
        assert out.outcome == vs.UNCHECKED_UNREACHABLE, code


def test_transient_codes_are_separated():
    """上游忙碌重跑就好，與「這個來源打不開」是不同的行動——不可混計。"""
    for code in ("too_many_requests", "unavailable"):
        out = _one(["https://a.example/1"], [],
                   [_attempt("https://a.example/1", ok=False, error_code=code)])
        assert out.outcome == vs.UNCHECKED_TRANSIENT, code


def test_unknown_error_code_falls_to_other():
    """SDK 之後新增錯誤碼時要落到 other，不可靜默混進額度或來源那兩桶。"""
    out = _one(["https://a.example/1"], [],
               [_attempt("https://a.example/1", ok=False, error_code="brand_new_code")])
    assert out.outcome == vs.UNCHECKED_OTHER


# ── 無 attempt 的兩種分流（T009，混淆會讓 US3 得出錯誤結論）────────────

def test_no_attempt_with_budget_exhausted_is_budget():
    """額度已用完 → 這則根本沒機會被查。這是 8/13 以來每天都在發生的那一格。"""
    out = _one(["https://a.example/1"], [], [], limit=3, used=3)
    assert out.outcome == vs.UNCHECKED_BUDGET
    assert out.attempted is False


def test_no_attempt_with_budget_left_is_model_choice():
    """額度還有卻沒去開＝模型**主動放棄**查證，與額度不足是完全不同的問題。"""
    out = _one(["https://a.example/1"], [], [], limit=3, used=1)
    assert out.outcome == vs.UNCHECKED_OTHER


def test_frugal_mode_zero_limit_is_not_budget_failure():
    """節儉模式沒掛工具（limit=0）——那是「沒有查證這回事」，不是額度為 0 的失敗。"""
    out = _one(["https://a.example/1"], [], [], limit=0, used=0)
    assert out.outcome == vs.UNCHECKED_OTHER


# ── FR-006：模型自述 vs 工具行為 ────────────────────────────────────────

def test_claimed_confirmed_without_successful_fetch_is_not_checked():
    """8/20 實際發生：模型說 confirmed，工具卻沒有對應的成功紀錄。

    採信自述就會把「沒查證」記成「已查證」——那正是 FR-006 禁止的事。
    """
    out = _one(["https://a.example/1"], [_check("https://a.example/1", "confirmed")],
               [], limit=3, used=3)
    assert out.outcome == vs.UNCHECKED_BUDGET
    assert out.claimed_unbacked is True
    assert out.verdict == "confirmed"     # 自述照樣保留，只是不採信


def test_fact_check_without_matching_clue_is_flagged():
    """模型裁決了一個召回層沒給過的來源——本身就是訊號，不可靜默吞掉。"""
    outcomes = vs.classify_outcomes(
        [], [_check("https://rogue.example/x", "confirmed")], [],
        fetch_limit=3, fetch_requests=0,
    )
    assert len(outcomes) == 1
    assert outcomes[0].from_facts is False


# ── Edge Cases（spec 明列）─────────────────────────────────────────────

def test_same_source_backing_multiple_verdicts_is_counted_once():
    """實測發生過：一個來源頁支撐 3 則裁決。重複計次會灌大成功率。"""
    url = "https://news.example/story"
    outcomes = vs.classify_outcomes(
        [url, url], [_check(url, "confirmed"), _check(url, "confirmed")],
        [_attempt(url), _attempt(url)],
        fetch_limit=3, fetch_requests=2,
    )
    assert len(outcomes) == 1
    assert outcomes[0].outcome == vs.CONFIRMED


def test_url_differences_that_do_not_matter_still_match():
    """尾斜線／大小寫不同就判成「沒查過」，會把成功的查證記成失敗。"""
    out = _one(["https://News.Example.com/story/"], [],
               [_attempt("https://news.example.com/story")])
    assert out.outcome == vs.CHECKED_INSUFFICIENT
    assert out.domain == "news.example.com"


def test_www_prefix_difference_still_matches():
    """`www.` 是最常見的無意義差異：召回層拿 www、模型 fetch 裸網域（或反過來）。

    配不上的後果不是少一筆統計——是把一則**真的查證成功**的線索判成 claimed_unbacked，
    然後被 guardrail 從 news_digest / sources 刪掉。誤刪正確內容比漏報難察覺得多。
    """
    url = "https://www.cnyes.com/news/id/1"
    out = _one([url], [_check(url, "confirmed")],
               [_attempt("https://cnyes.com/news/id/1")])
    assert out.outcome == vs.CONFIRMED
    assert out.attempted is True
    assert out.claimed_unbacked is False
    assert out.domain == "cnyes.com"


def test_same_path_different_query_must_not_share_an_attempt():
    """query 常常就是文章 id：`?id=1` 與 `?id=2` 是兩篇不同報導。

    寬鬆比對若忽略 query，沒查的那則會繼承查過那則的成功紀錄，被認證成 confirmed
    並取得引用資格——在 fail-closed 的 guard 裡開了一個 fail-open 的洞。
    """
    a, b = "https://n.example/article?id=1", "https://n.example/article?id=2"
    outcomes = vs.classify_outcomes(
        [a, b], [_check(b, "confirmed")], [_attempt(a)],
        fetch_limit=6, fetch_requests=1,
    )
    by_url = {o.url: o for o in outcomes}
    assert by_url[a].attempted is True
    assert by_url[b].attempted is False, "b 從未被開啟，不得繼承 a 的成功紀錄"
    assert by_url[b].outcome not in vs.CHECKED
    assert by_url[b].claimed_unbacked is True


def test_tracking_params_still_match_when_unambiguous():
    """但寬鬆比對本身要留著：同 path 只有一個 URL 時，utm 之類的差異仍該吸收掉。"""
    out = _one(["https://n.example/article?utm_source=x"], [],
               [_attempt("https://n.example/article")])
    assert out.attempted is True


def test_one_success_beats_a_previous_failure_on_same_url():
    """同一個 URL 開兩次、一次成功就算查過了（第二次成功不該被第一次失敗蓋掉）。"""
    url = "https://a.example/1"
    out = _one([url], [_check(url, "confirmed")],
               [_attempt(url, ok=False, error_code="too_many_requests"), _attempt(url)])
    assert out.outcome == vs.CONFIRMED


def test_no_clues_at_all_does_not_explode():
    """召回 0 則時分母為 0——不得當機，也不得顯示成 0%。"""
    assert vs.classify_outcomes([], [], [], fetch_limit=3, fetch_requests=0) == []
    assert vs.summarize([]) == {}
    assert vs._ratio(0, 0) is None


def test_clue_accepts_object_dict_or_plain_url():
    """線上路徑給 FactEvent 物件、回放給 dict、測試給字串——三種都要吃得下。"""
    class _Event:
        url = "https://a.example/1"
        claim = "台積電 8 月營收年增 30%"

    from_obj = _one([_Event()], [], [_attempt("https://a.example/1")])
    from_dict = _one([{"url": "https://a.example/1", "claim": "x"}], [],
                     [_attempt("https://a.example/1")])
    from_str = _one(["https://a.example/1"], [], [_attempt("https://a.example/1")])
    assert from_obj.claim.startswith("台積電")
    assert from_dict.outcome == from_str.outcome == from_obj.outcome


# ── 跨報告彙總（T012、FR-005、SC-002）──────────────────────────────────

def _write_report(tmp_path, name, cost):
    (tmp_path / name).write_text(json.dumps({"cost": cost}, ensure_ascii=False),
                                 encoding="utf-8")


def _clue(outcome, domain="a.example", error_code=None, claimed=False):
    return {"url": f"https://{domain}/x", "domain": domain, "outcome": outcome,
            "error_code": error_code, "claimed_unbacked": claimed, "verdict": None,
            "attempted": bool(error_code), "from_facts": True}


def test_aggregate_answers_sc002(tmp_path):
    """SC-002：額度不足 + 來源打不開要涵蓋 95% 以上的失敗案例。"""
    _write_report(tmp_path, "morning_20260831_082000.json", {
        "decision_provider": "anthropic", "frugal_mode": False,
        "verification": {"clues": [
            _clue(vs.CONFIRMED),
            _clue(vs.UNCHECKED_BUDGET),
            _clue(vs.UNCHECKED_UNREACHABLE, domain="blocked.example",
                  error_code="url_not_accessible"),
        ]},
    })
    summary = vs.aggregate(tmp_path)
    assert summary["days"] == 1 and summary["clues"] == 3
    assert summary["failures_n"] == 2
    assert summary["sc002_coverage"] == 1.0
    assert summary["budget_share"] == 0.5
    assert summary["error_codes"] == {"url_not_accessible": 1}
    assert summary["domains"]["blocked.example"]["fail"] == 1


def test_aggregate_excludes_polluted_samples(tmp_path):
    """轉址 bug 期、降級供應商、節儉模式都會污染基準，必須排除（spec Assumptions）。"""
    good = {"decision_provider": "anthropic", "frugal_mode": False,
            "verification": {"clues": [_clue(vs.CONFIRMED)]}}
    _write_report(tmp_path, "morning_20260807_082000.json", good)          # 轉址 bug 期
    _write_report(tmp_path, "morning_20260901_082000.json",
                  {**good, "decision_provider": "gemini-fallback"})        # 降級
    _write_report(tmp_path, "morning_20260902_082000.json",
                  {**good, "frugal_mode": True})                           # 節儉
    _write_report(tmp_path, "morning_20260903_082000.json", good)          # 唯一該計入的
    summary = vs.aggregate(tmp_path)
    assert summary["days"] == 1 and summary["clues"] == 1
    assert summary["skipped"] == {"redirect_bug": 1, "fallback": 1, "frugal": 1}


def test_aggregate_tolerates_old_reports(tmp_path):
    """舊報告沒有 clues 欄位：不得炸、也不得混進新格式的分母（會讓未查證憑空變少）。"""
    _write_report(tmp_path, "morning_20260813_082000.json", {
        "decision_provider": "anthropic", "frugal_mode": False,
        "verification": {"facts_n": 8, "fact_checks_n": 7, "fetch_requests": 3,
                         "verdicts": {"unverifiable": 7}},
    })
    _write_report(tmp_path, "morning_20260814_082000.json", {})            # 連 cost 都沒有
    summary = vs.aggregate(tmp_path)
    assert summary["days"] == 0 and summary["clues"] == 0
    assert summary["adjudication_rate"] is None                            # 分母 0 不是 0%
    assert summary["legacy"]["days"] == 1
    assert summary["legacy"]["facts"] == 8 and summary["legacy"]["fetches"] == 3
    assert vs.render(summary)                                              # 印得出來不炸


def test_aggregate_counts_claimed_unbacked(tmp_path):
    _write_report(tmp_path, "morning_20260831_082000.json", {
        "decision_provider": "anthropic", "frugal_mode": False,
        "verification": {"clues": [_clue(vs.UNCHECKED_BUDGET, claimed=True)]},
    })
    assert vs.aggregate(tmp_path)["claimed_unbacked_n"] == 1
