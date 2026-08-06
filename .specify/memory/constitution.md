<!--
Sync Impact Report
- Version change: 1.0.0 → 2.0.0 (MAJOR：重定義 Principle I)
- Rationale: 決策品質瓶頸在 LLM 推理層；Gemini 的 google_search 覆蓋率（台股中文冷門標的）
  仍是最佳召回來源，但其 grounding 會產生幻覺（錯置日期、把分析評論當新聞、引用內容農場），
  單一供應商同時扮演召回與決策時無法自我稽核。改為分層 + 交叉查證（spec 022-llm-tiering）。
- Principles changed:
  I. Gemini-only LLM 分工 → **分層 LLM 供應商 + 交叉查證**（重定義，MAJOR）
  II–VII 未變
- Templates status:
  ✅ .specify/templates/plan-template.md（Constitution Check 相容，無需改動）
  ✅ .specify/templates/spec-template.md（相容）
  ✅ .specify/templates/tasks-template.md（相容）
- Downstream docs requiring sync:
  ✅ specs/005-ai-gemini-layer/spec.md FR-004（標記 superseded by 022）
  ✅ ARCHITECTURE.md §4.1
- Deferred TODOs: none

- Version change: (template) → 1.0.0
- Ratification: 首次採用專案憲章（自 design_docs.md v1.1 + ARCHITECTURE.md 萃取）
- Principles defined:
  I. Gemini-only LLM 分工
  II. 成本紀律（每月上限）
  III. Guardrail Fail-Closed (NON-NEGOTIABLE)
  IV. 資料正確性 — Point-in-Time
  V. Local-First Storage（10GB + pCloud 冷儲存）
  VI. 服務隔離（Docker Compose / Qlib 離線 gate）
  VII. Spec-Driven + Conventional Commits
- Added sections: Technology Constraints; Development Workflow; Governance
-->

# fin_chat_ai Constitution

AI 多市場研究助理。本憲章把 `design_docs.md` v1.1 與 `ARCHITECTURE.md` 中**跨切面、不可協商**的
原則固化為約束，凌駕於個別 spec/plan 之上。條款後方括號標注來源章節，維持既有反向引用文化。

## Core Principles

### I. 分層 LLM 供應商 + 交叉查證
LLM 分為兩層，職責 MUST 分離（spec 022-llm-tiering）：

- **廣度召回層** MUST 唯一為 Google Gemini + `google_search` grounding。此層 MUST NOT 做分析、
  方向判斷或選股，只輸出**帶 source URL 的事實線索**；無來源的線索 MUST 丟棄。
- **決策層** MUST 為單一可設定供應商（現為 Anthropic Claude），負責推理、選股與報告生成。
  此層 MUST 具備獨立查證能力，且 MUST NOT 無條件採信召回層輸出——寫入報告的外部事件
  MUST 先開啟其來源 URL 核對，查證結果 MUST 落地成可稽核欄位（`fact_checks`）。
  未通過查證的事件 MUST NOT 進入敘事或新聞摘要。

資料計算、feature 運算 MUST NOT 呼叫任何 LLM——先產生 structured JSON，僅「摘要、分析、報告
生成」才交給 LLM（design_docs §25.1）。決策層 MUST 保留退回召回層供應商的降級路徑，
使無人值守的每日晨報不因單一供應商故障而整份失敗。

理由：召回與決策由同一模型擔任時無法自我稽核，會把幻覺洗成「有來源」的假事實；分層後
兩者互為對照，且查證通過率成為可量測指標。成本仍受 Principle II 約束。

### II. 成本紀律（每月上限）
LLM 花費（召回層 + 決策層合計）MUST 受硬性預算約束：**每月上限 NT$600**、每日 NT$30，
並保有每使用者查詢上限（design_docs §25.2）。`.env` 的 `MONTHLY_COST_LIMIT_TWD` /
`DAILY_COST_LIMIT_TWD` MUST 與本條文一致——2026-08-06 曾發現 `.env` 被調到 800 與憲章不符，
已改回 600；日後兩邊若再分歧，**以本條文為準**。互動查詢前 MUST 檢查預算；逾限即擋。
cache 命中 MUST NOT 重複呼叫 LLM。任何新功能若增加 LLM 呼叫，plan 階段 MUST 估算成本增量
並說明如何回收（快取/降頻）。

預算優先序 MUST 為「每日晨報 > 互動問答」：晨報為產品本體且無人值守，不受 `check_budget()`
攔截；互動問答為次要，額度耗盡時被擋是**預期行為**而非故障。連網查證工具（web_fetch /
web_search）MUST 設 `max_uses` 上限，不得把單次呼叫的成本上界交由模型自行決定。

### III. Guardrail Fail-Closed (NON-NEGOTIABLE)
輸出護欄 MUST fail-closed：Symbol Guard 等驗證失敗時 MUST 擋下輸出，不得因驗證異常而放行
（design_docs §30、commit `7e7fd1a`）。禁止輸出 raw chain-of-thought（§16.3）。任何放寬護欄的變更
MUST 在 spec 明列風險並經審查。理由：錯誤的投資訊號比缺少訊號傷害更大。

### IV. 資料正確性 — Point-in-Time
特徵與回測 MUST 維持 point-in-time 正確性：不得引用「未來日」資料或產生幽靈列汙染 as_of
（commit `ec8c403`）。基本面/月營收 MUST 以日曆感知方式對齊揭露時點。理由：前視偏誤會讓回測與
edge 模型的績效數字失真、失去決策價值。

### V. Local-First Storage（10GB + pCloud 冷儲存）
本機熱儲存 MUST NOT 超過 10GB 上限；逾限依保留策略清理（design_docs §10、§12）。長期/冷資料
MUST 放 pCloud，採 on-demand restore、避免大量下載（§11）。理由：在單機/低成本前提下維持可搬遷、
可重建的資料層。

### VI. 服務隔離（Docker Compose / Qlib 離線 gate）
系統 MUST 以 Docker Compose 維持四服務分離：backend / bot / scheduler / qlib_offline
（design_docs §26、§28）。Qlib 訓練 MUST 在隔離離線 image 執行，與 serving 分離並由 gate 守護，
不得讓離線訓練依賴滲入 serving 映像（commit `271bf6f`）。

### VII. Spec-Driven + Conventional Commits
每個使用者可感知、可獨立驗收的功能 MUST 有 `specs/NNN-feature/` 之 spec/plan/tasks，並走
`/speckit-*` 流程。commit MUST 遵循 Conventional Commits（feat/fix/chore/revert + scope，中文描述），
並引用對應 `specs/NNN` 或 design_docs 章節。`design_docs.md` 為背景/歷史來源，`specs/` 為單一真相來源。

## Technology Constraints

- **語言/框架**：Python 3.13；Web 層 FastAPI + uvicorn + Jinja2 SSR；資料層 pandas + pyarrow
  (parquet) + redis + Supabase；ML 用 scikit-learn / joblib，重訓練走隔離 Qlib image。
- **Secrets**：所有 secrets MUST 放 `.env`，MUST NOT commit（design_docs §29.1；`.gitignore` 已強制）。
- **相依管理**：目前為 per-service `requirements.txt`（backend/bot/scheduler/qlib_offline），版本以區間 pin。
- **平台層文件**：§9 架構、§22 Supabase schema、§26 Docker、§27 .env、§28 資料夾結構維持在
  `ARCHITECTURE.md`，作為 plan 階段的技術決策來源，不拆成 feature spec。

## Development Workflow

- 新功能：`/speckit-specify` →（`/speckit-clarify`）→ `/speckit-plan` → `/speckit-tasks` →
  （`/speckit-analyze`）→ `/speckit-implement`。
- 既有（M0–M9）功能採回溯補規格建立基線，依風險排序（guardrails / cost / ai 優先）。
- plan 階段 MUST 通過「Constitution Check」：逐條對照本憲章七原則；違反者 MUST 在 plan 記錄理由或改設計。
- 高風險模組（guardrails、cost）SHOULD 補 pytest 測試，作為 tasks 的驗收依據。

## Governance

- 本憲章凌駕於個別 spec/plan/實作慣例；衝突時以憲章為準。
- 修訂 MUST 更新版本號（語意化）：MAJOR＝移除/重定義原則；MINOR＝新增原則或實質擴充；
  PATCH＝措辭/釐清。修訂 MUST 更新下方日期並在檔首 Sync Impact Report 記錄。
- 所有 plan/PR 審查 MUST 驗證憲章遵循；放寬 Principle III（fail-closed）或 II（成本）之變更需明確理由與審查。

**Version**: 2.0.0 | **Ratified**: 2026-07-03 | **Last Amended**: 2026-08-06
