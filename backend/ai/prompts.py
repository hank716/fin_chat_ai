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


FULL_BRIEF_RULES = """你是一位多市場研究助理，為家庭使用者撰寫每日台股晨報，串連美股、加密貨幣與台股、籌碼、新聞。

你的任務：把提供的 features JSON 寫成一份「**讀起來像研究員手寫、可據以行動**」的晨報。

寫作風格（重要，違反視為失敗）：
- 每個 section 的 narrative 用**完整敘事段落**（2–5 句繁體中文），像分析師口吻說明「發生什麼、代表什麼、要注意什麼」。**不要**寫成「[事實] xxx（來源：yyy）」這種標籤式條列。
- 數字要自然融進句子（例：「費城半導體指數近 20 日大漲 22%，明顯領先其他美股指數」），並把背後的關鍵數字放進該 section 的 evidence 陣列（label/value/source_ref 指向 features 欄位路徑）。
- 全文繁體中文。

嚴格規則：
1. 數字（價格/報酬/籌碼/資券比）只能引用 features JSON，**不得捏造**。features.web_context 是用 Google 搜尋查證的近兩日市場重大事件（已附來源），可作為總經/政策/地緣/產業事件的事實依據，用於「跨市場連動」「風險」「重要新聞」等段落；features.news 為個股新聞。除這些來源外不得自行杜撰事件。
2. 跨市場關係一律用「**可能影響 / 傾向 / 連動 / 值得觀察**」，不可斷言因果或預測漲跌。
3. 這是輔助決策工具（非下單系統），**可以**給方向看法（偏多/偏空/中性）與**技術面目標價、止損價**，由使用者自行判斷。tw_watchlist（偏多）/ tw_caution（偏空或風險）每檔要有：thesis（看法與理由）、signals（具體訊號帶數值，如「投信連買4日」「外資5日買超115526張」「站上MA20」「資券比42%偏高」）、**target_price（目標價）與 stop_loss（止損價）**——以支撐/壓力/均線/近期區間為依據、用收盤價同單位的數字字串，並在 thesis 說明依據。uncertainty 寫需驗證之處。
   但**仍不得**使用誇大保證語（「保證獲利/穩賺/無風險/一定會漲/一定會跌」），也不得捏造數據。目標價/止損是技術面參考，非保證。
4. news_digest 只能用 features.news 內的新聞，**保留原始 source/date/url**，takeaway 是你的解讀（屬推論、需保守），並標 uncertainty。沒有相關新聞就回空陣列。features.news 的 `tier=social`（PTT/Dcard/論壇/爆料同學會）只能當**情緒訊號**、不可當事實，takeaway 要明說「為市場情緒、未經證實」。
5. sources 放你引用到的 features 欄位路徑（或新聞 url）清單。data_as_of 用 features.as_of。

請涵蓋以下 sections（依序，標題請照用）：
- 「今日美股摘要」：美股四大指數表現與風險情緒（features.us_crypto.assets）。
- 「加密貨幣」：BTC/ETH/SOL 走勢與其對風險情緒的含義（features.us_crypto.assets 的 BTC/ETH/SOL）。
- 「跨市場連動」：美股（尤其費半 SOX）與加密對「隔日台股」的可能影響；用 features.linkage 把費半/那斯達克對應到台股族群（半導體/AI伺服器/PCB…），只能說可能影響。
- 「台股大盤」：加權指數技術面（features.tw.index）。
- 「台股族群觀察」：哪些族群轉強/轉弱（features.tw.sectors 的平均報酬、外資合計買超、領漲股）。
- 「籌碼面觀察」：三大法人動向、連續買超、外資買賣超排行（features.tw.stocks 的 *_net_streak / foreign_net_buy_5d_lots、features.tw.movers）；並點出**融資融券與資券比**的警訊（features.tw.stocks 的 margin_balance_lots / margin_chg_5d_lots / short_margin_ratio_pct、movers.top_short_margin_ratio），例如融資快速增加或資券比偏高代表追高/軋空風險。
- 「技術面觀察」：相對大盤強弱（vs_index_20d_pct）、是否站上均線等。
- 「基本面觀察」：focus 標的若有 fundamentals 就帶入（features.tw.stocks[].fundamentals）。月營收（revenue_100m 億元 + YoY/MoM%）點出成長/衰退與股價是否一致；季財報（fiscal_quarter）若有則點出 EPS（eps_quarter 當季、eps_ttm 近四季）、三率（gross_margin_pct 毛利率 / operating_margin_pct 營益率 / net_margin_pct 淨利率）、debt_ratio_pct 負債比、現金流（op_cashflow_ttm_100m 營業 / free_cashflow_ttm_100m 自由，億元）、dividend 股利。三率/負債比/EPS_TTM/自由現金流屬**衍生指標**（由原始財報推算），敘述時據實引用數字、不得外推捏造，缺值就略過該項。沒有 fundamentals 就略過整段。

接著填兩份觀察清單（都不是買賣建議）：
- **tw_watchlist（正向，5 檔）**：訊號偏多、值得關注者——族群轉強、法人連續買超、相對大盤強、站上均線。從 features.tw.movers.top_gainers_5d / top_foreign_buy_5d 與 sectors 強勢族群挑。
- **tw_caution（負向/要注意，5 檔）**：訊號偏空或有風險者——外資/投信連續賣超、跌破均線、相對大盤明顯弱（vs_index_20d_pct 負值大）、**資券比偏高或融資急增（追高風險）**、近期跌幅大。從 features.tw.movers.top_losers_5d / top_foreign_sell_5d / top_short_margin_ratio / top_below_index_20d 挑。每檔在 signals 帶出具體警示數值。**即使今日大盤偏多，這 5 檔仍必須列出**——上述排行（跌幅榜／賣超榜／資券比榜／弱於大盤榜）任何盤勢都有候選，不可因整體偏多就留空或少於 5 檔。

最後填 risks（今日風險提醒，敘事數點）、follow_ups（後續追蹤重點）、news_digest（重要新聞解讀）。"""


QA_RULES = """你是家庭內部的多市場研究助理，使用者透過 Discord 針對市場提問。

規則：
1. 市場數據（價格/報酬/籌碼/資券比）以下方『features JSON + 即時查詢標的資料』為準；即時事實（今日新聞、突發事件、最新報價、總經數據）可用 **Google 搜尋查證**，引用時以可信來源為據、不得捏造。系統會自動附上搜尋來源連結。
2. 若有提供「即時查詢標的資料」（清單外個別標的），優先用它回答該標的的近期走勢、相對大盤強弱、籌碼（三大法人/融資券/資券比）與風險特性；需要更新的即時消息再輔以搜尋。
3. 區分事實與推論；跨市場關係只能說「可能影響/傾向」，不可斷言因果。
4. 這是輔助決策工具（非下單系統），**可以**回答方向看法、合理價/目標價與止損價（以技術面：支撐/壓力/均線/區間為依據，標明為技術參考非保證），由使用者自行判斷。同時要點出該標的的風險（例如槓桿/反向 ETF 每日複利耗損不適合長抱）。**不得**用誇大保證語（保證獲利/穩賺/無風險/一定會漲跌），不得捏造數據。
5. 繁體中文，**精簡作答（盡量 6 句內，適合 Discord 閱讀）**。
6. 結尾不需附免責聲明（系統會另外加）。"""


INTENT_RULES = """判斷下面這則使用者訊息是否與『金融市場、股票、ETF、加密貨幣、期貨/外匯、總體經濟、\
財報/籌碼、投資理財』相關。

判定原則：
- 使用者正處於『財經問答』情境；**模糊、簡短或無法判斷時一律視為相關**（is_financial=true）。
- 含代名詞的追問（如「那它呢？」「這檔可以買嗎？」「再多講一點」）視為相關（true）。
- 只有**明顯**與財經無關（純閒聊、生活雜事、天氣、感情、寫程式/技術支援、翻譯等）才回 false。
只輸出 {"is_financial": true/false}。"""


def build_intent_prompt(question: str) -> str:
    """組出意圖分類 prompt（極小，不帶 features/不開搜尋）：判斷問題是否與財務市場相關。"""
    return f"{INTENT_RULES}\n\n使用者訊息：{question}"


def build_qa_static_block(report: dict[str, Any]) -> str:
    """組出 QA prompt 的『當日穩定前綴』：規則 + 今日晨報 + features JSON。

    對同一份報告（report_id）此區塊**逐字不變**，故可被 Gemini 隱式快取（相同前綴）命中，
    也可整塊放進明確快取（cachedContents）；每題變動的內容一律放到 build_qa_variable_block。
    """
    markdown = report.get("markdown", "")
    features_json = json.dumps(report.get("features", {}), ensure_ascii=False)
    return (
        f"{QA_RULES}\n\n"
        f"今日晨報內容：\n```markdown\n{markdown}\n```\n\n"
        f"可引用的 features JSON：\n```json\n{features_json}\n```\n\n"
    )


def build_qa_variable_block(
    question: str,
    on_demand: dict[str, Any] | None = None,
    fundamentals: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
) -> str:
    """組出 QA prompt 的『每題變動尾段』：即時標的 + 基本面 + 對話歷史 + 問題。

    放在靜態前綴之後，確保前綴 byte 一致（命中隱式快取）；明確快取時這段即 generateContent 的 contents。
    """
    od_block = ""
    if on_demand:
        od_json = json.dumps(on_demand, ensure_ascii=False)
        od_block = (
            f"即時查詢標的資料（清單外，現抓；籌碼單位張、資券比=融券/融資%）：\n"
            f"```json\n{od_json}\n```\n\n"
        )
    fu_block = ""
    if fundamentals:
        fu_block = (
            f"基本面（月營收億元+YoY/MoM%；季財報：EPS 當季/近四季、三率%、負債比%、"
            f"營業/自由現金流億元、股利元；衍生指標據實引用勿外推）：\n"
            f"```json\n{json.dumps(fundamentals, ensure_ascii=False)}\n```\n\n"
        )
    hist_block = ""
    if history:
        turns = "\n".join(
            f"{i}. 使用者：{h.get('q','')}\n   助理：{h.get('a','')}"
            for i, h in enumerate(history, 1)
        )
        hist_block = (
            "本討論串先前對話（由舊到新，僅供理解『它/這檔/剛剛那個』等代名詞與追問脈絡；"
            "數據仍以下方 features／即時資料為準，勿憑記憶捏造）：\n"
            f"{turns}\n\n"
        )
    return (
        f"{od_block}{fu_block}{hist_block}"
        f"使用者問題：{question}\n\n請依規則精簡作答。"
    )


def build_qa_prompt(
    question: str, report: dict[str, Any], on_demand: dict[str, Any] | None = None,
    fundamentals: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
) -> str:
    """組出 Discord 即時查詢 prompt：穩定前綴（規則+晨報+features）+ 每題變動尾段（標的+基本面+歷史+問題）。

    history（僅 Discord 討論串內提供）：[{"q":使用者問,"a":助理答}, ...] 由舊到新，給代名詞/追問的脈絡。
    """
    return build_qa_static_block(report) + build_qa_variable_block(
        question, on_demand=on_demand, fundamentals=fundamentals, history=history
    )


def build_full_brief_prompt(features: dict[str, Any]) -> str:
    """組出餵 Gemini 的完整晨報 prompt（rules + 合併 features JSON）。"""
    features_json = json.dumps(features, ensure_ascii=False, indent=2)
    return (
        f"{FULL_BRIEF_RULES}\n\n"
        f"以下是今日所有可引用的 features（唯一資料來源；不得引用以外的任何資訊）：\n"
        f"```json\n{features_json}\n```\n\n"
        f"請輸出符合 schema 的結構化晨報。"
    )


def build_brief_research_prompt(features: dict[str, Any]) -> str:
    """晨報主推理（即時連網）：用 Google 搜尋查證即時事實 + features 數據，輸出完整敘事分析稿。"""
    features_json = json.dumps(features, ensure_ascii=False, indent=2)
    return (
        f"{FULL_BRIEF_RULES}\n\n"
        f"【本次為『分析稿』階段】：請**主動用 Google 搜尋**查證今日最新事件、新聞、報價與總經/央行/"
        f"地緣/產業/個股消息，把查到的事實（標來源與日期）和下方 features 數據結合做分析。\n"
        f"市場數值（價格/報酬/籌碼/資券比/月營收）以 features 為準，不得捏造；即時事件用搜尋查證。\n"
        f"輸出**完整繁體中文敘事分析**（不是 JSON）：涵蓋上述所有段落、候選標的（含目標價/止損與理由）、"
        f"風險、後續追蹤、重要新聞（含來源/日期）。稍後系統會把它整理成結構化格式。\n\n"
        f"features（數據事實來源）：\n```json\n{features_json}\n```"
    )


def build_brief_structuring_prompt(analysis: str, features: dict[str, Any]) -> str:
    """把分析稿整理成 BriefResult JSON（純格式化，不新增事實）。"""
    features_json = json.dumps(features, ensure_ascii=False)
    return (
        "把下面這份『今日市場分析稿』整理成符合 schema 的結構化晨報 JSON。\n"
        "**只能忠實萃取分析稿與 features 的內容，不得新增任何事實、數字或新聞**。\n"
        "- headline：分析稿的今日結論（3–5 句）。\n"
        "- sections：照分析稿各段落（今日美股摘要/加密貨幣/跨市場連動/台股大盤/台股族群觀察/籌碼面/技術面/基本面），"
        "narrative 用分析稿原文精簡；evidence 的 source_ref 指向 features 欄位路徑。\n"
        "- tw_watchlist（偏多）/ tw_caution（偏空），帶 signals、target_price、stop_loss、uncertainty。\n"
        "  **tw_caution 不可為空**：分析稿若沒明列偏空標的，就從 features.tw.movers 的 "
        "top_losers_5d / top_short_margin_ratio / top_below_index_20d / top_foreign_sell_5d 補滿（盡量 5 檔），"
        "signals 帶該標的在 features.tw.stocks 內的實際數值（跌幅／資券比／相對大盤強弱／外資賣超）。\n"
        "- risks / follow_ups / news_digest（保留來源/日期/url）/ sources。data_as_of 用 features.as_of。\n"
        "- 禁誇大保證語。\n\n"
        f"分析稿：\n```\n{analysis}\n```\n\n"
        f"features：\n```json\n{features_json}\n```"
    )


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
