# Implementation Plan: 查證閉環有效性

**Spec**: [spec.md](./spec.md) | **Branch**: `023-verification-efficacy` | **Created**: 2026-08-12

## Summary

把「查證失敗」從一個混合判定拆成可稽核的分類，先量測、後調參。

核心技術發現：**Anthropic SDK 已經把我們需要的答案結構化回傳了**，只是現行程式沒讀。
`web_fetch_tool_result` block 的 `content` 是一個 union——成功時是 `WebFetchBlock`
（帶 `url`、`retrieved_at`），失敗時是 `WebFetchToolResultErrorBlock`（帶 `error_code`）。
`error_code` 的值域直接對應 spec 的兩個競爭假說：

| `error_code` | 對應假說 | 意義 |
|---|---|---|
| `max_uses_exceeded` | **A：額度不足** | 撞到 `max_uses`，根本沒查 |
| `url_not_accessible` / `url_not_allowed` / `unsupported_content_type` / `url_too_long` | **B：來源打不開** | 查了但這個來源開不起來 |
| `too_many_requests` / `unavailable` | 暫時性 | 上游忙碌，重跑可能就好 |
| `url_not_in_prior_context` | 召回層缺陷 | 模型想開一個對話裡沒出現過的 URL |
| `invalid_tool_input` | 決策層缺陷 | 模型送了畸形參數 |

所以 FR-006（結局 MUST 由系統從實際工具行為推導、不得依賴模型自述）不但可行，
而且比現行「讀模型 `note` 的中文猜」精確得多——**這是本案能成立的關鍵前提**。

## Technical Context

**Language/Version**: Python 3.11（backend 容器）

**Primary Dependencies**: `anthropic` SDK（已在用；本案只是多讀既有回應欄位）、FastAPI、Jinja2

**Storage**: 報告 JSON 落地於 `storage/reports/*.json`（Local-First，憲章 V）；無新增儲存體

**Testing**: pytest（`tests/`，現 174 passed）；容器內跑

**Target Platform**: Linux container（`docker compose` 的 `backend` 服務）

**Project Type**: 單一後端服務 + 排程 + Discord bot

**Constraints**: 憲章 II 成本上限（月 NT$800 / 日 NT$45）；單篇晨報現均值 NT$32.5，
只夠每日一篇。US1/US2 為零成本增量（純讀既有回應、純渲染）；US3 才會動成本。

**Scale/Scope**: 每日 1 篇晨報 × 5–10 則線索；統計樣本以「交易日」計，非高流量

## Constitution Check

*GATE：Phase 0 前必過，設計後複查。*

| 原則 | 狀態 | 說明 |
|---|---|---|
| I. 分層 LLM + 交叉查證 | ✅ **本案即在修復此原則的實作** | 稽核閉環目前 78% 的時候沒有真的閉合；本案讓失效可被偵測 |
| II. 成本紀律 | ✅ US1/US2 零增量；⚠️ US3 條件性 | US1/US2 只讀既有回應欄位與渲染，不多打一次 API。US3 必須以 US1 資料為依據（憲章 II 明訂），且 FR-012 要求月投影不得超限 |
| II. max_uses 調整需遙測依據 | ✅ **本案就是在建那個遙測** | 這條是 2.1.0 為此情境新增的，US1 是它的直接落實 |
| II. 降級 MUST 對使用者可見 | ✅ US2 | 把「未查證」納入既有的 `degradation_notes` 機制（2.1.0 新增條文） |
| III. Guardrail Fail-Closed | ✅ FR-009 | 未查證線索不得被當已查證事實引用；沿用 `run_guardrails` 的擋下語意 |
| IV. Point-in-Time | ✅ 不受影響 | 完全不碰本地特徵／回測／訓練資料層 |
| V. Local-First Storage | ✅ | 統計資料從既有報告 JSON 彙總，不新增外部儲存 |
| VI. 服務隔離 | ✅ | 僅動 `backend`；`qlib_offline` / `bot` / `scheduler` 不變 |
| VII. Spec-Driven | ✅ | 本 spec 即 023 |

**無違規，無需 Complexity Tracking。**

## 架構決策

### 資料流

```
Claude 回應 content blocks
  ├─ server_tool_use(name=web_fetch, id=X, input={url})   ← 想開哪個 URL
  └─ web_fetch_tool_result(tool_use_id=X, content=...)    ← 開的結果
        ├─ WebFetchBlock            → ok,  url, retrieved_at
        └─ WebFetchToolResultErrorBlock → fail, error_code
                    │
       以 tool_use_id 配對 → FetchAttempt[]
                    │
   morning_brief 以 URL 對回 fact_checks[] → 每則線索的結局分類
                    │
        ├─ 報告 JSON: cost.verification（擴充）
        ├─ degradation.py → md / Discord / 網頁（US2）
        └─ 彙總 CLI → 跨報告統計（SC-002）
```

### 關鍵決策

| 決策 | 理由 |
|---|---|
| **從 content blocks 配對，不讀模型 note** | FR-006。模型自述無法稽核，且有動機把沒查證的講得像查過。這也是 8/10–8/12 只能靠讀中文猜的根因 |
| **失敗時的 URL 取自 `server_tool_use.input`** | 錯誤 block 沒有 `url` 欄位，只有 `error_code`；必須靠 `tool_use_id` 回頭配對才知道是哪個來源失敗——而「哪個來源」正是 US4 判斷的依據 |
| **新增獨立回傳通道，不塞進 `usage`** | `usage` 是 `dict[str, int]` 且在 `morning_brief` 被逐鍵相加；塞 list 進去會讓型別註記說謊，也會在相加時炸掉。多回一個值比污染既有結構誠實 |
| **續跑（`pause_turn`）要累加 attempts** | 同一次呼叫可能跨多輪，每輪都有自己的 content blocks。既有的 `web_fetch_requests` 計數已處理此情境，attempts 沿用同一個累加點 |
| **結局分類放在 `morning_brief`，不放 `claude_client`** | `claude_client` 是供應商轉接層，只該回報「工具發生了什麼」；「這則線索算不算查證成功」是業務判斷，屬報告層 |
| **彙總走離線 CLI 讀 JSON，不建即時 API** | 樣本是每日 1 篇、看的是趨勢不是即時值。Local-First（憲章 V），也避免為了一個維運查詢加 endpoint |
| **不在本案調整 `max_uses`** | 憲章 II 要求遙測依據；US3 是資料就緒後的獨立決策點，硬排進來就是重蹈 8/07 那次無依據調參 |

### 結局分類（FR-001 / FR-002）

每則線索落入且僅落入一類：

| 分類 | 判定依據 | 算「已核對」嗎 |
|---|---|---|
| `confirmed` | 模型判定 + 該 URL 有成功的 fetch attempt | ✅ |
| `contradicted` | 同上 | ✅ |
| `checked_insufficient` | 模型判 unverifiable **且** 該 URL 有成功 fetch | ✅（有效查證結果） |
| `unchecked_budget` | 該 URL 的 attempt 失敗於 `max_uses_exceeded`，或**根本沒有 attempt** 且該篇已耗盡額度 | ❌ |
| `unchecked_unreachable` | attempt 失敗於 `url_not_accessible` / `url_not_allowed` / `unsupported_content_type` / `url_too_long` | ❌ |
| `unchecked_transient` | attempt 失敗於 `too_many_requests` / `unavailable` | ❌ |
| `unchecked_other` | 其餘 error_code，或無 attempt 且額度未耗盡（模型自己選擇不查） | ❌ |

「無 attempt 且額度未耗盡」這一格特別重要：它代表模型**主動放棄查證**，
與額度不足是完全不同的問題，混在一起會讓 US3 得出錯誤結論。

## Project Structure

### Documentation

```text
specs/023-verification-efficacy/
├── spec.md
├── plan.md              # 本檔
├── tasks.md             # /speckit-tasks 產出
└── checklists/
    └── requirements.md
```

依 019 / 022 的既有慣例，本案不產 `research.md` / `data-model.md` / `contracts/` /
`quickstart.md`：無外部介面契約（純內部後端）、無新資料儲存體、技術未知數已在上表解決
（SDK 型別已實地確認）。驗收步驟寫在 tasks.md 而非獨立 quickstart。

### Source Code（實際會動到的檔案）

```text
backend/
├── ai/
│   └── claude_client.py        # 配對 server_tool_use ↔ web_fetch_tool_result，回傳 attempts
├── reports/
│   ├── morning_brief.py        # 結局分類、擴充 cost.verification
│   ├── degradation.py          # US2：未查證的提示（既有模組，擴充）
│   └── verification_stats.py   # 新增：跨報告彙總（CLI 入口）
├── guardrails/
│   └── verify.py               # FR-009：未查證線索不得被當已查證事實引用
└── templates/report.html       # US2 網頁面（既有 degradation 迴圈，可能不需改）

tests/
├── test_claude_client.py       # attempts 配對、續跑累加、錯誤碼分流
├── test_verification_stats.py  # 新增：結局分類與彙總
└── test_degradation_notes.py   # US2 新增案例
```

**Structure Decision**: 沿用既有單一 backend 服務結構。唯一新檔是
`reports/verification_stats.py`（彙總邏輯 + CLI），其餘皆為既有模組的擴充。
`degradation.py` 是上一輪（成本盤點）剛建立的共用模組，本案直接沿用不重造。

## 分期與依賴

| 階段 | 內容 | 依賴 | 成本增量 |
|---|---|---|---|
| **A** | US1 可觀測性（attempts 配對 → 結局分類 → 落地 JSON） | 無 | 0 |
| **B** | US2 誠實性（三個介面顯示未查證 + guardrail 擋引用） | A 的分類欄位 | 0 |
| **C** | 觀察窗：累積 ≥10 個交易日 | A+B 已部署 | 0 |
| **D** | US3 額度調整（**條件性**） | C 的資料顯示額度為主因 | 待估，受 FR-012 約束 |
| **E** | US4 召回層來源偏好（**條件性**） | C 的資料顯示來源系統性失敗 | 0–小 |

A 與 B 可同一次部署。**C 是硬性等待**，不能靠加班縮短——SC-002 要求 10 個交易日。
D 與 E 在 C 之前不得動工；若 C 的資料顯示某假說不成立，對應階段應**取消**而非硬做。

## 要接受的副作用

- **報告 JSON 的 `cost.verification` 結構會擴充**（新增每則線索的結局分類與 attempts 摘要）。
  舊報告沒有這些欄位，彙總與渲染都必須容忍缺欄位——`degradation.py` 現有的
  `test_missing_cost_block_is_tolerated` 已釘住這個要求，新程式沿用同樣的寬容度。
- **短期內成功率數字會變難看**。目前 22% 是「confirmed + contradicted」的比率；
  拆分後 `checked_insufficient` 會從 unverifiable 裡分出來計入「已核對」，
  真實的「有效查證率」可能更高，但「未查證率」也會第一次被明確標出來。
  這是量測本來就該有的效果，不是退步。
- **`unchecked_*` 的線索在 US2 之後會明顯減少報告的「外部事件」觀感**。若某日全數未查證，
  報告會呈現得像「本篇無外部事件」——這正是 FR-008 要的，但視覺衝擊要有心理準備。
