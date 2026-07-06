"""Q&A 輕量 guardrail（WP3.2）。

問答回答是自由文字、不像晨報有結構化 evidence，無法逐欄驗證；改做兩件低誤傷的事：
1. **禁語 redact**：誇大保證（保證獲利…）與必然因果（一定漲…）就地移除（對齊晨報 ADVICE/CAUSALITY）。
2. **疑似不存在代號標注**：文中的台股代號若不在 universe∪已知∪即時查，附一行提醒（不刪原文）。

為守「正常回答不變」，代號偵測刻意保守：純數字 4–6 位若像**年份**(1990–2099)或**整數**
(價格/數量/點位，%100==0)一律不視為代號，避免把「2026 年」「5000 萬」誤標。純函式、可測。
"""
from __future__ import annotations

import re
from typing import Any

from guardrails.verify import ADVICE_BANNED, CAUSALITY_BANNED

_TW_CODE = re.compile(r"(?<!\d)(\d{4,6}[A-Z]?)(?!\d)")
_REDACTION = "〔已移除誇大保證用語〕"


def _is_suspect_code(sym: str, allowed: set[str]) -> bool:
    """sym 是否為「疑似不存在的股票代號」（在 allowed 內、或像年份/整數者一律否）。"""
    if sym in allowed:
        return False
    if re.fullmatch(r"\d{4,6}", sym):          # 純數字才套年份/整數濾除；含字母後綴(ETF)一律視為代號
        n = int(sym)
        if 1990 <= n <= 2099:                  # 年份
            return False
        if n % 100 == 0:                        # 整數：價格/數量/點位（5000 萬、3000 點…）
            return False
    return True


def scan_answer(answer: str, allowed_symbols: set[str]) -> tuple[str, dict[str, Any]]:
    """回 (cleaned_answer, report)。禁語就地 redact；疑似不存在代號附一行標注（不刪原文）。"""
    cleaned = answer or ""
    banned: list[str] = []
    for w in (*ADVICE_BANNED, *CAUSALITY_BANNED):
        if w in cleaned:
            banned.append(w)
            cleaned = cleaned.replace(w, _REDACTION)

    unknown: list[str] = []
    for sym in dict.fromkeys(_TW_CODE.findall(answer or "")):   # 去重保序
        if _is_suspect_code(sym, allowed_symbols):
            unknown.append(sym)
    if unknown:
        cleaned += (f"\n\n⚠️ 提及的代號 {'、'.join(unknown)} 不在系統資料範圍，"
                    f"相關數據請自行查證。")

    report = {
        "banned_phrases": banned,
        "unknown_symbols": unknown,
        "clean": not banned and not unknown,
    }
    return cleaned, report
