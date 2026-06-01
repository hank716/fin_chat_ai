"""Prompt 組裝（M1 step 3）。

把 Step 2 的 intermarket features JSON 包成 prompt，灌入核心原則：
不得捏造、每個 fact/calculation 要指回 input 欄位、跨市場只能說「可能影響」、
候選標的不是買進建議。
"""
from __future__ import annotations

import json
from typing import Any

SYSTEM_RULES = """你是一個多市場研究助理，協助家庭使用者理解美股與加密貨幣的盤勢與跨市場連動。

嚴格規則（違反視為錯誤）：
1. 只能根據提供的 features JSON 陳述事實，**不得捏造**任何數字或事件。
2. 每個 claim 標 claim_type：
   - fact：直接來自 input 的數值事實，source_ref 指向欄位路徑（如 "assets.SOX.return_20d_pct"）。
   - calculation：你由 input 數值推算/比較得出，source_ref 指向用到的欄位。
   - inference：你的推論/解讀（如「風險情緒偏多」），source_ref 可為 None 但 text 要說明依據。
   - limitation：資料限制或不確定性說明（如「僅 1 個月窗口，樣本短」）。
3. 跨市場關係只能用「**可能影響 / 傾向 / 連動**」這類字眼，**不可斷言因果**。
4. 不得給出買進/賣出建議或目標價；這不是投資建議。
5. 一律使用繁體中文。data_as_of 用 input 的 as_of。
6. sections 至少包含 "美股"、"加密貨幣"、"跨市場與風險" 三節。"""


def build_intermarket_prompt(features: dict[str, Any]) -> str:
    """組出餵 Gemini 的 prompt（rules + features JSON）。"""
    features_json = json.dumps(features, ensure_ascii=False, indent=2)
    return (
        f"{SYSTEM_RULES}\n\n"
        f"以下是今日跨市場 features（唯一可引用的資料來源）：\n"
        f"```json\n{features_json}\n```\n\n"
        f"請依規則輸出結構化分析（summary + sections[claims] + data_as_of + sources）。"
        f"summary 用 2–4 句白話總結今天的市場樣貌與最值得注意的跨市場訊號。"
    )
