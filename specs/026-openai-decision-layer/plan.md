# Implementation Plan: 決策層新增 OpenAI 供應商（方案 B）

**Branch**: `026-openai-decision-layer` | **Date**: 2026-10-21 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/026-openai-decision-layer/spec.md`

## Summary

在不動召回層（Gemini + Google 搜尋）與預設決策供應商（Claude）的前提下，新增 OpenAI 決策實作，
並用「同日影子模式」累積 5–10 天可配對的比較資料。

三個關鍵技術決策（細節見 [research.md](./research.md)）：

1. **查證工具在本地執行、名稱沿用 `web_fetch`**：OpenAI 沒有可限制 URL 的抓頁工具，所以宣告一個 function tool，
   由 backend 自己抓頁，白名單＝當篇 facts pack 的 URL，並擋私有位址（SSRF）。錯誤碼沿用 spec 023 值域，
   `verification_stats` 分類邏輯零改動；prompt 一字不改，A/B 只差模型本身。
2. **計價第三條分支**：OpenAI GPT-5.6+ 的 input 分「未命中 / cached read / cache write」三種互斥費率，
   `input_tokens` 是總數 → 未命中 = input − cached − write。與 Anthropic、Gemini 的語意都不同。
3. **影子在發布之後跑、寫進子目錄**：主晨報照常 08:30 發布；影子用同一份 features + facts 再跑一次 OpenAI，
   存到 `reports/shadow/`。所有現行 glob 都是非遞迴的 `morning_*.json`，影子天然不會被網頁、Discord、Supabase 撿到。
   跑之前檢查預算，確保影子不會把後續主晨報推進節儉模式。

## Technical Context

**Language/Version**: Python 3.12（`backend/Dockerfile` 為 `python:3.12-slim`；憲章 Technology Constraints 寫 3.13，屬既有文件落差，非本案範圍）

**Primary Dependencies**: 新增 `openai>=3.35,<3.36`（Responses API）、`beautifulsoup4>=4.12,<5`（容器已有，補明確 pin）；沿用 `httpx` 0.27、`anthropic`、pydantic、FastAPI

**Storage**: 報告 JSON 檔（`/data/reports/`，影子在 `/data/reports/shadow/`）；redis 成本桶新增 `cost:month:YYYYMM:openai`

**Testing**: pytest（`docker compose exec -T backend pytest`），全程 mock，無需 API key；基線 543 passed

**Target Platform**: Docker Compose 上的 backend 服務（Windows 主機，單機）

**Project Type**: web-service（FastAPI backend + Discord bot + scheduler）

**Performance Goals**: 主晨報產出時間不變（影子在發布後才跑）；影子單次 ≤ 10 分鐘

**Constraints**: 月上限 NT$800（試驗期 NT$1,000）、日 45；`/ask` 延遲不因本案變差；無 key 環境必須能啟動

**Scale/Scope**: 每日 1 篇晨報 + 1 篇影子，試驗 5–10 個交易日；約 6 個檔案修改、4 個新模組、5 個新測試檔

## Constitution Check

*GATE: Phase 0 前檢查、Phase 1 設計後複查。*

| 原則 | 判定 | 說明 |
|---|---|---|
| I. 分層 LLM + 交叉查證 | ✅ 需 MINOR 修訂 | 召回層仍唯一為 Gemini；決策層本來就是「單一可設定供應商」，加 OpenAI 是擴充值域。查證閉環保留且更嚴（程式強制白名單）。降級路徑仍退回 Gemini（跨供應商備援不變）。影子模式讓同日有兩個決策供應商在跑，但**只有一個發布**——條文需補一句明文允許「不發布的影子對照」。→ 憲章 2.1.0 → **2.2.0** |
| II. 成本紀律 | ✅ 需 MINOR 修訂 | **成本增量**：影子每篇約 NT$12（gpt-6.1-sol 估），試驗期月增約 NT$250，月總額約 NT$820，超過 800。**回收方式**：若切換成立，往後每篇省約 NT$14、月省約 NT$290，約 1 個月回本；不成立則試驗到期影子自動停，損失上限約 NT$120（10 天）。依 Governance「放寬成本原則需明確理由與審查」，條文明訂試驗期上限 1,000 且須有結束日。查證與搜尋都有上限（`openai_brief_fetch_uses` / `openai_brief_search_uses`），符合 `max_uses` 要求。OpenAI 失敗的降級沿用現行「對使用者可見」機制 |
| III. Guardrail Fail-Closed | ✅ | 決策輸出走同一個 `run_guardrails`；影子也跑 guardrail（用於比較違規數），不放寬任何規則。reasoning 內容不外露（Responses API 只回 encrypted reasoning，不進報告） |
| IV. Point-in-Time | ✅ | 不碰特徵與回測 |
| V. Local-First Storage | ✅ | 影子 JSON 不含 features（~數十 KB/篇），試驗期合計 < 5 MB |
| VI. 服務隔離 | ✅ | 只改 backend；不新增服務；qlib 無關 |
| VII. Spec-Driven | ✅ | 本 spec/plan/tasks；commit 用 `feat(026): ...` |

**複查（Phase 1 後）**：設計沒有引入新的違反。唯一需注意的是試驗期日上限的副作用（`/ask` 當日剩約 NT$7），已寫進 quickstart，屬預期行為（憲章 II：問答被擋是預期而非故障）。

## Project Structure

### Documentation (this feature)

```text
specs/026-openai-decision-layer/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── decision-llm.md
│   └── provider-compare-cli.md
├── checklists/requirements.md
└── tasks.md              # /speckit-tasks 產出
```

### Source Code

```text
backend/
├── ai/
│   ├── openai_client.py       # 新：Responses API、strict schema、工具迴圈、usage、錯誤映射
│   ├── fetch_tool.py          # 新：FetchSession（白名單、SSRF、轉址、截斷）+ FetchAttempt 搬來這裡
│   ├── claude_client.py       # 改：FetchAttempt 改為 re-export
│   ├── llm_client.py          # 改：OpenAIDecisionLLM、get_decision_llm("openai")
│   └── prompts.py             # 不改（工具名沿用 web_fetch / web_search）
├── api/ask.py                 # 改：openai 分支、model_used
├── cost/tracker.py            # 改：_PRICING_OPENAI、第三條計價分支、provider_of、PROVIDERS
├── reports/
│   ├── morning_brief.py       # 改：decision_model 依 provider、fetch_limit 依 provider、影子步驟
│   ├── shadow_brief.py        # 新：run_shadow(feats, facts, calibration, report_id) → 寫 shadow/
│   └── provider_compare.py    # 新：比較 CLI
├── config.py                  # 改：openai_* 設定
└── requirements.txt           # 改：openai、beautifulsoup4

tests/
├── test_openai_client.py      # 新
├── test_fetch_tool.py         # 新
├── test_cost_openai.py        # 新
├── test_shadow_brief.py       # 新
├── test_provider_compare.py   # 新
└── test_provider_switch.py    # 改：加 openai 案例

.specify/memory/constitution.md  # 2.2.0：Principle I 影子條款、Principle II 試驗期上限
.env.example / README.md / ARCHITECTURE.md
```

**Structure Decision**: 沿用既有 `backend/ai`（供應商 client）＋ `backend/reports`（晨報流程與 CLI）分工。影子邏輯獨立成 `shadow_brief.py`，`morning_brief.py` 只加一個呼叫點，避免這支 780 行的檔案再膨脹，也讓影子在試驗結束後容易整段移除。

## 實作順序（交給 /speckit-tasks 細化）

1. **基礎**：config 欄位、requirements、`FetchAttempt` 搬家（re-export，跑既有測試確認零回歸）。
2. **US3 計價**（最先做，因為其他所有東西都要靠它記帳）：tracker OpenAI 分支 + 測試。
3. **US2 fetch_tool**：白名單、SSRF、轉址、截斷、額度 + 測試。
4. **US1 openai_client + OpenAIDecisionLLM**：schema、工具迴圈、錯誤映射、降級整合；`morning_brief` 的 `decision_model` / `fetch_limit` 依 provider。
5. **US4 影子 + 比較 CLI**。
6. **US5 /ask**。
7. **治理**：憲章 2.2.0、README / ARCHITECTURE / `.env.example`。

## 風險與緩解

| 風險 | 緩解 |
|---|---|
| `input_tokens` 是否含 cache write 與文件理解不同 → 計價偏差 | 首篇真跑立即對帳（SC-004）；公式集中在一個函式，改一行 |
| 本地抓頁對 JS 動態頁、擋爬蟲站失敗率高於 Anthropic 伺服器 | 錯誤碼照實記錄；`provider_compare` 的裁決率門檻（≥ 80%）會把這個差異算進去，抓頁弱就不會建議切換 |
| strict schema 讓 Optional 欄位全變 nullable，模型回 null 導致 pydantic 驗證失敗 | 驗證前剝除 null 的非 nullable 鍵；單元測試覆蓋 `BriefDraft` 全欄位 |
| 影子拖慢主流程或吃掉主晨報預算 | 發布後才跑；先檢查預算預留；整段 try/except |
| OpenAI SDK 3.x 與文件不符（欄位名） | 讀 usage 一律 `getattr(..., 0)` 容錯；`tests` 用 SimpleNamespace 模擬，key 到手首篇檢查 raw usage |

## Complexity Tracking

無需記錄——Constitution Check 無未解釋的違反。
