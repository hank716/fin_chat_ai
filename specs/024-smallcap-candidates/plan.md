# Implementation Plan: 小型股候選池

**Spec**: [spec.md](./spec.md) | **Created**: 2026-10-13

## Summary

分兩階段。**第一階段（本次）不動決策層**：候選池、排序、訊號文字全部由本機確定性產生，
成本與提示詞不變，也沒有幻覺風險。第二階段再讓決策層為入選個股撰寫說明（FR-010 的後半）。

## 第一階段的資料流

```
tw_features._smallcap_pool          帶內 5 日漲幅榜 ∪ 外資買超榜（各前 8）——與回測候選定義相同
        │  （prompts._budget_features 會把這塊從決策層輸入拿掉）
        ▼
smallcap_sleeve.build               四道關卡：開關 → 線上停損 → 交易限制名單 → 模型門檻
        │  sc._score_rank_band(band="smallcap") 取前 3；sc.score_risk 附回撤風險
        ▼
report["tw_smallcap"]               picks / reason / excluded_restricted；Markdown、網頁、Discord 各一段
        ▼
backtest.evaluate_report            → scorecard["smallcap_items"]（不併入 items，避免污染校準與訓練）
        ▼
evaluation.json["smallcap"]         獨立成效；同一份統計供線上停損判定
```

## 設計決策

1. **候選定義照抄回測**（`training_set._emit_samples` 的偏多側）：排序力是在這個池子上量到的。
2. **訓練帶跟著改成 1,000 萬–5,000 萬**（`SMALLCAP_MIN_TWD` 讀 `settings.smallcap_min_amount_twd`）：
   線上打分的個股與訓練樣本同分布。
3. **小型股明細放在 `features.tw.smallcap`**，不混進 `features.tw.stocks`：後者是決策層的選股範圍
   與 Symbol Guard 的依據，混進去等於允許決策層把小型股挑進主名單。
4. **scorecard 用獨立的鍵**：`strategy_calibration` 掃 `items` 時把非 watchlist 的一律當偏空。
5. **交易限制名單 fail-closed**：三個來源任一失敗就整份視為不可用、當天不列（憲章 III 的精神）。
6. **線上停損**：累積 20 個到期交易日後，名單逐日平均超額的跨日平均為負 → 停列並寫明原因。
   樣本不足時放行。恢復需人工檢視後調整（刻意不自動恢復，避免在門檻邊緣反覆開關）。

## Constitution Check

- **I 分層與交叉查證**：第一階段不經 LLM，無關。✅
- **II 成本紀律**：零 LLM 成本；每篇多 3 個交易所公開資料請求，與候選基本面查詢（有快取）。✅
- **III Guardrail Fail-Closed**：名單不可用、模型未過門檻、停損觸發時一律不列並說明原因。✅
- **IV Point-in-Time**：候選與打分只用當日或之前的資料；回測用 purged walk-forward。✅
- **VI 服務隔離**：無新服務。✅

## 回測依據（SC-004）

資料修復後、447 個樣本外交易日、每日前 3 檔、5 日持有：

| 流動性帶 | 選股 alpha | 贏同池日比 | 扣 0.6% 成本後淨報酬 |
|---|---|---|---|
| 500 萬–5,000 萬 | +1.49% | 56.8% | +1.58% |
| **1,000 萬–5,000 萬（採用）** | **+1.44%** | 54.1% | **+1.54%** |

以新帶重訓的小型股 rank 模型：5 日 rank-IC 0.0533（ICIR 0.208）、20 日 0.0288，過門檻 0.03。

## 實跑驗證（2026-10-13，不經 LLM 的乾跑）

候選池 16 檔（成交額 1,144 萬–4,843 萬），排除 1 檔處置股（6225），列出 3 檔；
15/16 檔取得基本面。Markdown 段落與 scorecard 路徑由測試涵蓋。

## 已知限制

- 上櫃變更交易（全額交割）名單尚無可用的公開端點，未排除。
- 訊號文字是資料條列，沒有敘事說明——那是第二階段。
- 選股歸因顯示主池的「5 日漲幅榜」是輸家池；小型股帶內同一個榜之所以可用，是因為有模型在池內排序。
  上線後若線上成效不成立，停損條件會讓它自動停列。
