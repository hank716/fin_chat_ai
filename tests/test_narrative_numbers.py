"""spec 021：候選說明裡的數字須與 features 相符。

句型全部取自已落地晨報的實際寫法；ENTRY 是對應的資料真值。
"""
from __future__ import annotations

from datetime import date

import pytest

from ai.schemas import BriefResult, WatchItem
from guardrails import narrative_numbers as nn
from guardrails.verify import run_guardrails

ENTRY = {
    "close": 38.8, "return_1d_pct": -2.02, "return_5d_pct": 5.15, "return_20d_pct": -65.2,
    "volatility_20d_pct": 1.75, "dist_ma20_pct": -12.79, "dist_ma60_pct": 14.85,
    "turnover_surge": 4.31, "vs_index_5d_pct": 3.16, "vs_index_20d_pct": -27.25,
    "foreign_net_buy_1d_lots": 29422, "foreign_net_buy_5d_lots": 58335, "foreign_net_streak": 7,
    "trust_net_buy_1d_lots": 10282, "trust_net_streak": -15, "dealer_net_streak": 9,
    "margin_chg_5d_lots": 821, "short_margin_ratio_pct": 48.56,
    "fundamentals": {"revenue_yoy_pct": 133.2, "revenue_mom_pct": -4.2, "gross_margin_pct": 66.2,
                     "eps_ttm": -7.22, "eps_quarter": 1.67, "debt_ratio_pct": 71.5},
}

# SC-002：數字正確（含四捨五入、方向詞、省略主詞、千分位）
CORRECT = [
    "外資連買7日、5日買超58,335張",
    "收盤38.8元、距MA20 -12.79%",
    "單日股價重挫2.02%，跌破月線12.79%",
    "20日大跌65.2%、20日相對大盤落後27.25個百分點",
    "投信連賣15日（反向警訊）",
    "8月營收年增133.2%、月減4.2%",
    "20日波動度約1.8%",                      # 1.75 四捨五入到一位
    "資券比高達48.56%、融資5日續增821張",
    "近四季EPS -7.22元、負債比71.5%",
    "單日+9.91%、量能放大4.31倍".replace("+9.91", "-2.02"),
]

# SC-001：數字被改過（值錯、方向反、主詞錯位）
FABRICATED = [
    "外資連買7日、5日買超85,335張",            # 位數對調
    "單日上漲2.02%",                          # 方向相反
    "20日大漲65.2%",                          # 方向相反
    "距MA20 +12.79%",                         # 正負號相反
    "投信連買15日",                           # 連賣寫成連買
    "8月營收年增233.2%",                      # 值錯
    "20日波動度3.5%",                         # 值錯
    "資券比8.56%",                            # 值錯
    "近四季EPS 7.22元",                       # 虧損寫成獲利
    "外資連買2日",                            # 日數錯
]


def _mismatched(text: str) -> bool:
    return any(not c.ok for c in nn.extract_claims("2883", text, ENTRY))


def test_fabricated_numbers_are_flagged():
    flagged = sum(_mismatched(t) for t in FABRICATED)
    assert flagged >= 8, [t for t in FABRICATED if not _mismatched(t)]


def test_correct_numbers_are_not_flagged():
    wrong = [t for t in CORRECT if _mismatched(t)]
    assert len(wrong) <= 1, wrong
    # 正確句必須真的被核對到，而不是因為認不出來才沒報
    assert sum(len(nn.extract_claims("2883", t, ENTRY)) for t in CORRECT) >= 15


@pytest.mark.parametrize("text", [
    "所屬電子通路業20日平均亦為-2.59%",       # 族群數字
    "鋼鐵工業外資5日買超207,235張",           # 族群合計
    "費半5日重挫9.49%為主要逆風",             # 海外指數
    "目標價取月線上方約7%的46元",             # 目標價推估，不是乖離
    "高波動（20日15.58%）",                   # 括號切開後無從判斷指標（2026-09-07 回放的誤判）
    "融資5日反向激增42,948張至312,581張",     # 資料無此檔對應值時另案；此處驗不誤判成法人
])
def test_numbers_about_something_else_are_left_alone(text):
    entry = {**ENTRY, "margin_chg_5d_lots": 42948}
    assert not any(not c.ok for c in nn.extract_claims("2883", text, entry))


def test_institution_is_the_one_nearest_the_number():
    claims = nn.extract_claims("2883", "投信已連賣15日與外資對作", ENTRY)
    assert [(c.metric, c.ok) for c in claims] == [("trust_net_streak", True)]


def test_missing_data_is_not_checked():
    assert nn.extract_claims("2883", "單日上漲2.02%", {"return_1d_pct": None}) == []
    assert nn.extract_claims("2883", "單日上漲2.02%", {}) == []


def test_same_number_in_thesis_and_signals_counts_once():
    item = {"symbol": "2883", "thesis": "外資5日買超58,335張", "signals": ["外資5日買超58,335張"],
            "uncertainty": None}
    assert len(nn.check_item(item, ENTRY)) == 1


def _brief(thesis: str) -> BriefResult:
    item = WatchItem(symbol="2883", name="凱基金", thesis=thesis, signals=["自營商連買9日"])
    return BriefResult(headline="h", tw_watchlist=[item], data_as_of=date(2026, 10, 12))


def test_guardrail_warns_without_removing_or_rewriting():
    feats = {"tw": {"stocks": {"2883": ENTRY}}, "news": []}
    cleaned, report = run_guardrails(_brief("外資5日買超85,335張，單日下跌2.02%"), feats)
    assert cleaned.tw_watchlist[0].thesis == "外資5日買超85,335張，單日下跌2.02%"   # FR-005 不改寫
    nnum = report["narrative_number"]
    assert (nnum["checked"], nnum["mismatched"]) == (3, 1)
    assert nnum["mismatches"][0]["metric"] == "foreign_net_buy_5d_lots"
    assert report["passed"] is True and report["warning_count"] == 1                 # 只警示不阻斷


def test_validator_crash_is_visible_not_silent(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("regex bug")

    monkeypatch.setattr(nn, "check_item", boom)
    feats = {"tw": {"stocks": {"2883": ENTRY}}, "news": []}
    cleaned, report = run_guardrails(_brief("外資5日買超58,335張"), feats)
    assert len(cleaned.tw_watchlist) == 1                                            # 報告照常產出
    assert any(v["guard"] == "narrative_number" and v["severity"] == "error"
               for v in report["violations"])
