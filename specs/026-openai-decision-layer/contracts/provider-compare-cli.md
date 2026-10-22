# Contract: 對照比較指令

```
python -m reports.provider_compare [REPORTS_DIR] [--since YYYYMMDD] [--until YYYYMMDD] [--json]
```

- `REPORTS_DIR` 預設 `settings.local_storage_path/reports`；影子從 `REPORTS_DIR/shadow/` 讀。
- 路徑不存在或兩邊都 0 篇 → 非零 exit code 並印錯誤（不得印一張空表，比照 spec 023 `verification_stats` 修正）。
- 只比較**同一份主報告與其影子都存在**的配對；影子 `error` 不為空的配對另列「影子失敗 N 次」，不進平均。

## 輸出（文字模式）

```
配對天數：7（2026-10-22 ~ 2026-10-30），影子失敗 0 次

                        claude-opus-5     gpt-6.1-sol
單篇成本 平均/中位 NT$     25.8 / 25.4       11.9 / 11.6
裁決率                   0.42              0.38
guardrail errors（合計）  3                 2
output tokens 平均        18,300            14,900
  其中 reasoning 平均     —                 8,100
查證次數 平均             3.0               2.9

切換門檻（SC-005）
  成本 ≤ 60%       ✅ 46%
  裁決率 ≥ 80%     ✅ 90%
  違規不多於 Claude ✅
→ 三項成立：建議切換預設
```
（數字為示意。）

- 成本：主報告用 `cost.decision_twd + cost.retrieval_twd`；影子用 `cost.decision_twd + 主報告 retrieval_twd`（召回共用、只付一次，兩邊用同一個召回成本才公平）。
- 裁決率：`(confirmed + contradicted) / 線索數`，由 `verification_stats` 既有彙總函式算。
- `--json`：輸出同內容的機器可讀 JSON。
