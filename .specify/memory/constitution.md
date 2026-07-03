<!--
Sync Impact Report
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
- Templates status:
  ✅ .specify/templates/plan-template.md（Constitution Check 相容，無需改動）
  ✅ .specify/templates/spec-template.md（相容）
  ✅ .specify/templates/tasks-template.md（相容）
- Deferred TODOs: none
-->

# fin_chat_ai Constitution

AI 多市場研究助理。本憲章把 `design_docs.md` v1.1 與 `ARCHITECTURE.md` 中**跨切面、不可協商**的
原則固化為約束，凌駕於個別 spec/plan 之上。條款後方括號標注來源章節，維持既有反向引用文化。

## Core Principles

### I. Gemini-only LLM 分工
LLM 供應商 MUST 唯一為 Google Gemini；不得在 serving 路徑引入其他付費 LLM。資料計算、feature
運算 MUST NOT 呼叫 LLM——先產生 structured JSON，僅「摘要、分析、報告生成」才交給 Gemini
（design_docs §25.1）。若需 Claude/其他模型輔助分析，走「複製 prompt」離線流程，不接入 serving。
理由：成本可控、行為可預測、guardrail 可套用於單一輸出管線。

### II. 成本紀律（每月上限）
Gemini 花費 MUST 受硬性預算約束：每月上限 NT$600（現況 ~NT$66.76，見 README 成本現況），並保有
每日、每使用者查詢上限（design_docs §25.2）。查詢前 MUST 檢查預算；逾限即擋。cache 命中 MUST NOT
重複呼叫 Gemini。任何新功能若增加 LLM 呼叫，plan 階段 MUST 估算成本增量並說明如何回收（快取/降頻）。

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

**Version**: 1.0.0 | **Ratified**: 2026-07-03 | **Last Amended**: 2026-07-03
