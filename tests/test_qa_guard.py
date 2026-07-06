"""WP3.2：Q&A 輕量 guardrail——禁語 redact + 疑似代號標注，且正常回答不變。"""
from __future__ import annotations

from guardrails import qa_guard

ALLOWED = {"2330", "2317", "00919"}


def test_banned_phrase_redacted():
    ans = "這檔保證獲利，必漲。"                    # 保證獲利∈ADVICE、必漲∈CAUSALITY
    cleaned, rep = qa_guard.scan_answer(ans, ALLOWED)
    assert "保證獲利" not in cleaned and "必漲" not in cleaned
    assert "〔已移除誇大保證用語〕" in cleaned
    assert set(rep["banned_phrases"]) >= {"保證獲利", "必漲"}
    assert rep["clean"] is False


def test_unknown_symbol_annotated():
    ans = "可以留意 9999 這檔股票。"
    cleaned, rep = qa_guard.scan_answer(ans, ALLOWED)
    assert "9999" in rep["unknown_symbols"]
    assert "不在系統資料範圍" in cleaned          # 附標注、原文保留
    assert ans.split("。")[0] in cleaned


def test_normal_answer_unchanged():
    """含年份(2026)、整數價格(5000)、已知代號(2330) 的正常回答不得被更動或誤標。"""
    ans = "2026 年台積電(2330)展望正向，技術面看 5000 點附近支撐。"
    cleaned, rep = qa_guard.scan_answer(ans, ALLOWED)
    assert cleaned == ans
    assert rep["clean"] is True
    assert rep["unknown_symbols"] == [] and rep["banned_phrases"] == []


def test_year_and_round_numbers_not_flagged():
    cleaned, rep = qa_guard.scan_answer("2024 年到 2027 年，量能約 3000 萬、上看 12000 點。", set())
    assert rep["unknown_symbols"] == []           # 年份/整數皆非代號


def test_etf_alpha_suffix_code_flagged_when_unknown():
    cleaned, rep = qa_guard.scan_answer("00685L 這檔槓桿 ETF 風險高。", ALLOWED)
    assert "00685L" in rep["unknown_symbols"]     # 含字母後綴＝明確代號，不在 allowed → 標注


def test_known_symbol_not_flagged():
    cleaned, rep = qa_guard.scan_answer("00919 是高股息 ETF。", ALLOWED)
    assert rep["unknown_symbols"] == [] and rep["clean"] is True
