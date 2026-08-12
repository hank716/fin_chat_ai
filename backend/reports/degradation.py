"""晨報降級提示（憲章 II：降級 MUST 對使用者可見）。

晨報有三種「照常出報、但品質實質下降」的路徑，過去它們只存在於報告 JSON 的 cost 區塊
與一行 log——使用者實際閱讀的 markdown / Discord / 網頁上完全看不出來：

1. **節儉模式**（月餘額見底或當日已跑過一篇）：無外部事件、無連網查證、effort=low。
2. **決策層降級**（Claude 失敗退回 Gemini 兩段式）：完全不做查證，fact_checks 為空。
3. **查證層空轉**（工具掛了但一次都沒開）：verdict 看起來像查過，實際沒有來源支撐。

看不見的品質降級比降級本身更糟：月底連續一週拿到沒有外部事件的晨報，讀的人不會知道
那是降級版，會把它當成「今天真的沒事發生」。三個輸出面共用這裡的同一份文案，避免各寫一份
後漂移（也避免其中一個面漏掉某種降級）。
"""
from __future__ import annotations

from typing import Any


def degradation_notes(cost: dict[str, Any] | None) -> list[str]:
    """依報告的 cost 區塊回傳應顯示的降級提示（無降級時回空 list）。"""
    if not cost:
        return []

    notes: list[str] = []
    frugal = bool(cost.get("frugal_mode"))
    provider = str(cost.get("decision_provider") or "")
    verification = cost.get("verification") or {}

    if frugal:
        notes.append("本篇為預算節儉模式：無外部事件、無連網查證，僅依本地量化資料產出")
    if provider.endswith("fallback"):
        notes.append(f"決策層降級為 {provider}：本篇外部事件未經查證")
    # 節儉模式本來就沒工具，不重複報；只在「該查卻沒查」時才提示。
    if not frugal and verification.get("facts_n") and not verification.get("fetch_requests"):
        notes.append("查證層本次未實際開啟任何來源：判定結果無原文佐證，請自行查核")
    if verification.get("unadjudicated_n"):
        notes.append(
            f"{verification['unadjudicated_n']} 則外部線索未被裁決"
            f"（召回 {verification.get('facts_n')} 則、僅查證 {verification.get('fact_checks_n')} 則）"
        )
    return notes
