# Survivorship bias 稽核（WP1.4）

> 重跑：`LOCAL_STORAGE_PATH="$PWD/storage" PYTHONPATH=backend python -m reports.survivorship_audit`

## 結果（資料快照 2026-03-04 ～ 2026-06-02，約 3 個月）

| 指標 | 值 |
|------|----|
| parquet 有價股票數 | 2,363 |
| universe 快照股票數 | 2,728 |
| **P−U**（有價、不在 universe） | **0** |
| U−P（在 universe、無價） | 365（多為 ETF / 槓桿反向 00xxxL/R，及尚未回補者；**非** survivorship） |
| 訓練集來自 P−U 的樣本占比 | **0.0000%** |
| sector 查不到的樣本占比 | 0.0000% |

fwd_return 分布（全體）：h5 mean 2.51 / med 0.34；h20 mean 8.75 / med 2.79。P−U 無樣本可比。

## 判定：**記為已知限制、關閉（不開 spec 018）**

依計畫準則「P−U 樣本占比 < 2% → 關閉」：目前為 0%，每一檔有價股票都在當前 universe 快照內、
sector 全部查得到，`_ETF_SECTORS` 過濾正常。當前資料窗內**無可測的 survivorship 缺口**。

## ⚠️ 重要保留（必讀）

此結論**受限於資料只有 3 個月**。真正的 survivorship bias 是「在資料窗起點（2026-03）之前就已下市」
的股票——它們同時缺席於 parquet **與** universe 當前快照，因此本稽核**看不到**它們，0% 不代表長期無偏差。

**觸發重稽核的條件**：當 `history_crawl` 把 parquet 補深到 ~2 年後，2024–2026 間發生的下市事件會落在
資料窗內。屆時：
- 全市場逐日端點回補會保留下市股的歷史價（→ 進得了 parquet），但
- 當前 universe 快照**不含**已下市代號 → 這些股票 `sector_of()` 回 None → sector/sector_rs 特徵 NaN，
  且 `_ETF_SECTORS`（靠 sector 名判斷）對 sector=None 的下市 ETF 失效。

→ 補深資料後**必須重跑本稽核**；若屆時 P−U 樣本占比 ≥ 2%，再開 `spec 018-pit-universe`
（universe 每日快照 `tw_all_{date}.json` + `_symbol_long` 對查不到 sector 的股票 fallback 到最近一份
含該股的快照）。此為 [[fin-chat-ai-project]] 資料補深後的待辦。
