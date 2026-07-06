"""WP0.4：fundamentals._pick 科目比對——精確/別名命中、且絕不子字串誤配部分科目。"""
from __future__ import annotations

from processor import fundamentals as f


def test_exact_match_wins():
    items = {"Revenue": 100.0, "GrossProfit": 55.0}
    assert f._pick(items, "Revenue") == 100.0
    assert f._pick(items, "GrossProfit") == 55.0


def test_never_substring_matches_partial_account():
    """只有部分科目(CurrentLiabilities)、沒有整體(Liabilities/TotalLiabilities)時必須回 None。

    這正是舊子字串 fallback 的 bug：'Liabilities' 會誤命中 'CurrentLiabilities' → 低估負債。
    打破會 fail：若還原子字串 fallback，會回 50.0（部分負債），assert is None 紅掉。
    """
    items = {"CurrentLiabilities": 50.0, "NoncurrentLiabilities": 30.0}
    assert f._pick(items, "Liabilities", "TotalLiabilities") is None


def test_total_liabilities_exact_beats_partial():
    """FinMind 以 'Liabilities' 表示總負債；同時存在部分科目時，精確命中整體、不取部分。"""
    items = {"Liabilities": 200.0, "CurrentLiabilities": 50.0}
    assert f._pick(items, "Liabilities", "TotalLiabilities") == 200.0


def test_alias_resolves_concept_synonym():
    """整體概念的已知別名（OperatingRevenue ↔ Revenue）可解析。"""
    items = {"OperatingRevenue": 100.0}
    assert f._pick(items, "Revenue") == 100.0


def test_interfering_accounts_do_not_confuse_revenue():
    """含 'Revenue' 子字串的干擾細項不得影響對 Revenue 的精確取值。"""
    items = {"Revenue": 100.0, "TotalNonoperatingIncomeAndExpense": 5.0, "SomeRevenueDetail": 9.0}
    assert f._pick(items, "Revenue") == 100.0


def test_case_insensitive_exact():
    items = {"revenue": 100.0}
    assert f._pick(items, "Revenue") == 100.0


def test_missing_returns_none():
    assert f._pick({"GrossProfit": 10.0}, "Revenue") is None
    assert f._pick({}, "Revenue") is None
