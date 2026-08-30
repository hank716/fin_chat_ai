"""晨報降級提示（憲章 II：降級 MUST 對使用者可見）。

晨報有三種「照常出報、但品質實質下降」的路徑，過去它們只存在於報告 JSON 的 cost 區塊
與一行 log——使用者實際閱讀的 markdown / Discord / 網頁上完全看不出來：

1. **節儉模式**（月餘額見底或當日已跑過一篇）：無外部事件、無連網查證、effort=low。
2. **決策層降級**（Claude 失敗退回 Gemini 兩段式）：完全不做查證，fact_checks 為空。
3. **查證失敗**（額度用盡沒查、或來源打不開）：verdict 看起來像查過，實際沒有來源支撐。

第 3 項在 spec 023 之前只有一種粗糙的講法（「一次都沒開」）。實際上「額度不足所以沒查」與
「開了但這個來源打不開」是兩個不同的問題、要採取的行動也不同（前者調額度、後者換來源），
混成一句話等於兩邊都修不了。

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

    # 查證層只在「有掛 web_fetch 的主路徑」存在。節儉模式與降級供應商都**沒有查證這回事**，
    # 它們的降級已經各自有一句話講清楚了；再跑一次查證失敗分流只會疊出三、四句互相重複
    # 的提示（「未經查證」講三遍），反而稀釋掉真正的訊息。
    if _verification_ran(cost):
        notes.extend(_verification_notes(verification))
        if verification.get("unadjudicated_n"):
            notes.append(
                f"{verification['unadjudicated_n']} 則外部線索未被裁決"
                f"（召回 {verification.get('facts_n')} 則、"
                f"僅查證 {verification.get('fact_checks_n')} 則）"
            )
    return notes


def _verification_ran(cost: dict[str, Any]) -> bool:
    """這篇報告到底有沒有查證層可言。

    `verification_active` 是 spec 023 之後才落地的欄位；舊報告沒有，退回用
    `frugal_mode` + `decision_provider` 推導（那兩個欄位從 spec 022 起就一直都在）。
    """
    verification = cost.get("verification") or {}
    active = verification.get("verification_active")
    if active is not None:
        return bool(active)
    return not cost.get("frugal_mode") and not str(
        cost.get("decision_provider") or ""
    ).endswith("fallback")


def _verification_notes(verification: dict[str, Any]) -> list[str]:
    """查證失敗的分流提示（spec 023 US2 / FR-007、FR-008）。

    舊報告沒有 `outcomes` 欄位——那時只知道「開了幾次」。這種情況退回原本那句粗略提示，
    不要因為欄位缺席就讓舊報告一則提示都不顯示（那等於靜默把降級藏起來）。
    """
    # ⚠️ 文案不得帶 markdown 語法：網頁面是 `{{ note }}` 逐字跳脫輸出（templates/report.html），
    # 星號會原樣顯示。三個面共用同一份文案，就得遷就限制最多的那一個。
    outcomes = verification.get("outcomes") or {}
    if not outcomes:
        if verification.get("facts_n") and not verification.get("fetch_requests"):
            return ["查證層本次未實際開啟任何來源：判定結果無原文佐證，請自行查核"]
        return []

    notes: list[str] = []
    clues_n = sum(int(v) for v in outcomes.values())
    checked_n = int(verification.get("checked_n") or 0)
    budget_n = int(outcomes.get("unchecked_budget") or 0)
    unreachable_n = int(outcomes.get("unchecked_unreachable") or 0)
    other_n = clues_n - checked_n - budget_n - unreachable_n

    # FR-008：全數未查證時，呈現效果要等同「本篇無經查證外部事件」，
    # 而不是「本篇有 N 則外部事件」——後者會讓讀者以為那 N 則有事實基礎。
    if clues_n and not checked_n:
        notes.append(
            f"本篇無經查證的外部事件：{clues_n} 則外部線索全數未能核對原文，"
            "內文提及的外部事件請自行查核"
        )
    if budget_n:
        limit = verification.get("fetch_limit")
        suffix = f"（本篇查證上限 {limit} 次）" if limit else ""
        notes.append(f"{budget_n} 則外部線索因查證額度不足而未核對{suffix}")
    if unreachable_n:
        notes.append(f"{unreachable_n} 則外部線索的來源無法開啟，未取得原文佐證")
    if other_n > 0:
        # 不能因為「已經報了額度不足/打不開」就省略這句：那樣 8 則線索只會交代 6 則，
        # 剩下 2 則憑空消失——US2 要的是每一則都有去向，不是挑最大宗的講。
        notes.append(f"{other_n} 則外部線索未經查證（原因見報告 JSON 的 cost.verification）")
    if verification.get("claimed_unbacked_n"):
        # 最該讓讀者看到的一類：模型說「已查證」，但工具沒有對應的成功開啟紀錄。
        notes.append(
            f"{verification['claimed_unbacked_n']} 則裁決標為已查證，但無實際開啟來源的紀錄"
        )
    return notes
