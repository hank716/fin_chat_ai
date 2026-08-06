# Implementation Plan: LLM 分層 — Claude 決策 + 連網查證

**Spec**: [spec.md](./spec.md) | **Branch**: `022-llm-tiering` | **Created**: 2026-08-06

## Constitution Check

| 原則 | 狀態 | 說明 |
|---|---|---|
| I. 分層 LLM 供應商 + 交叉查證 | ✅ **本案即為此原則的實作** | 憲章 1.0.0→2.0.0 由本案觸發；舊版 Gemini-only 條文與本案衝突，已重定義 |
| II. 成本紀律（NT$600/月） | ⚠️ 需實測 | 決策層改 Opus 5 + 連網查證會顯著推高晨報成本（粗估 NT$250–450/月）。**上限不放寬**；預算優先序明訂為晨報 > 問答 |
| III. Guardrail Fail-Closed | ✅ 不受影響 | `run_guardrails` 輸入介面 `BriefResult` 未變 |
| IV. Point-in-Time | ✅ 不受影響 | 零 LLM 的本地特徵/回測層完全不動 |
| V. Local-First Storage | ✅ | facts pack 隨晨報 JSON 落地，體積小 |
| VI. 服務隔離 | ✅ | 僅動 backend；qlib_offline / bot / scheduler 不變 |
| VII. Spec-Driven | ✅ | 本 spec 即為 022 |

## 架構決策

```
[ features JSON (本地 parquet, 零 LLM) ]──┐
[ 回測校準 calibration ]─────────────────┼──> Claude Opus 5 ──> BriefDraft
[ Gemini facts pack + source URLs ]──────┘    ├ web_fetch  (開召回層引用的 URL 查證)
        ↑ 廣度召回，標記為「待查證」            ├ web_search (補漏 / 交叉比對)
                                              └ structured outputs（單次呼叫）
                                                          │
                                       本地 ML 打分/融合/sizing（完全不動）
```

**關鍵洞察**：`web_fetch` 只能抓取**已出現在對話裡的 URL**。這個限制正好是特性——決策層只能
去開召回層真的引用過的連結，無法自己生一個 URL 出來，天然形成稽核閉環。

## 技術決策與踩雷點

| 決策 | 理由 |
|---|---|
| 用官方 `anthropic` SDK，不用 raw httpx | Gemini 路徑走 raw REST 是歷史因素；SDK 內建重試/streaming/structured outputs，自己刻沒有價值 |
| **不**額外包 tenacity | SDK 已有 `max_retries`；`test_gemini_retry.py` 就是在防「3×4=12 次」的重複重試 bug，同一個坑不踩第二次 |
| Pydantic 模型直接當 schema | 現行 `GEMINI_BRIEF_SCHEMA` 用 `"nullable": True` 是 Google OpenAPI 方言，非合法 JSON Schema，無法平移 |
| draft 模型排除三個 ML 欄位 | `risk_score`/`conviction_score`/`size_weight` 由本地模型事後填；讓 LLM 產出只會幻覺一組數字再被覆蓋 |
| 必須 streaming | `max_tokens=32000` 非 streaming 會撞 SDK HTTP timeout（SDK 直接 raise ValueError） |
| 必須處理 `pause_turn` | server tool 迭代上限會回 pause_turn；SDK tool runner **不會**自動續跑，漏處理會得到靜默截斷的晨報 |
| `web_fetch` 不開 citations | 與 `output_config.format` 互斥，同開直接 400；來源追蹤走自訂 `fact_checks` |
| 不宣告 `code_execution` | `_20260209` 版內建 dynamic filtering，重複宣告會造成兩個執行環境 |
| prompt cache 預設**關閉** | 寫入 1.25×(5m)/2×(1h)、讀取 0.1×；chat 稀疏使用下每題都是「寫入後未被讀取就過期」＝純虧 |

## 成本

| 項目 | 現況 | 改後 |
|---|---|---|
| 晨報/篇 | ≈ NT$7.7 | 需實測；Gemini flash 召回 + Opus 5 決策 + web_fetch ×≤12 / web_search ×≤5 |
| 晨報/月（21 交易日） | ≈ NT$160 | 粗估 NT$250–450，**可接受**（晨報是產品本體） |
| /ask | Gemini flash + 明確快取 | Claude，用量稀疏、佔比小 |
| 月上限 | NT$600 | **不變** |

查證成本來自三處：server tool 按次計費、fetch 回來的網頁內容佔 input token（最大宗）、
pause_turn 續跑要重送整個對話。

**緩解順序（只在實測撞上 NT$600 才動，不傷晨報品質的先做）**：
features JSON 去 indent + `sort_keys` → `web_fetch` 的 `max_content_tokens` 收緊 →
chat 降 `claude-sonnet-5` → `MAX_BRIEF_STOCKS` 下修 → **最後**才動 `max_uses` / `effort`。

## 要接受的副作用

- **晨報不受 `check_budget()` 管**（現行閘門只擋 `/ask`）。晨報可能把月度額度吃到見底，
  接下來整月 chat 全被擋——這是刻意取捨。緩解：`/brief/latest` 的 `cost` 多回
  `month_remaining_twd`，讓超支不是無聲發生。
- **不要為省錢調低 `max_uses`**：模型會在查證到一半被切斷，產出「部分查證」的晨報，
  比不查證更糟——因為它看起來像查過了。
