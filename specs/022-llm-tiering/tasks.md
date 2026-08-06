# Tasks: LLM 分層 — Claude 決策 + 連網查證

**Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## WP0 — 憲章與文件（前置，否則後續違憲）

- [x] **T001** `.specify/memory/constitution.md`：Principle I 重定義為「分層 LLM 供應商 + 交叉查證」，
      Principle II 補預算優先序與 `max_uses` 要求；版本 1.0.0 → **2.0.0**，補 Sync Impact Report、
      更新 Last Amended。
- [x] **T002** `specs/005-ai-gemini-layer/spec.md`：FR-002 / FR-004 標記 superseded by 022。
- [x] **T003** `ARCHITECTURE.md` §4.1 改寫為分層架構 + 查證閉環說明。
- [x] **T004** 建立 `specs/022-llm-tiering/{spec,plan,tasks}.md`。

## WP1 — Provider 抽象層 + Anthropic client

- [x] **T005** 新增 `backend/ai/errors.py`：`LLMError` / `LLMUnavailable` / `LLMQuotaExceeded` /
      `LLMBadRequest`；`gemini_client` 的 `GeminiError` 家族改為繼承（既有 `except GeminiError` 不壞）。
- [x] **T006** `backend/requirements.txt` 加 `anthropic>=0.120,<0.121`。
- [x] **T007** `backend/config.py` + `.env.example`：新增 `anthropic_api_key` /
      `llm_decision_provider` / `claude_model_decision` / `claude_model_chat` / `claude_effort` /
      `enable_claude_prompt_cache`(預設 False) / `claude_cache_ttl` / `enable_facts_pack`；
      **並補回 `.env.example` 既有遺漏的 4 個 Gemini 欄位**。
- [x] **T008** 新增 `backend/ai/claude_client.py`：streaming + structured outputs + web_fetch/web_search
      + `pause_turn` 續跑迴圈 + refusal 檢查 + 例外映射。**不帶 temperature/top_p/top_k**。
- [x] **T009** 改寫 `backend/ai/llm_client.py` 為 `DecisionLLM` Protocol + `get_decision_llm()`
      分派，`AnthropicDecisionLLM` / `GeminiDecisionLLM` 兩實作。

## WP1b — 成本計價

- [x] **T010** `backend/cost/tracker.py`：`_PRICING_ANTHROPIC` + `_rates()` 先判 `claude-` 前綴
      （否則 `claude-opus-5` 會靜默落到 flash 費率、低估約 3 倍，而預算閘門正是靠它）。
- [x] **T011** per-provider usage 正規化：Anthropic `input_tokens` **不含** cache，
      不可套用 Gemini 的 `input - cached` 減法。
- [x] **T012** 新增 `cache_write_tokens`（1.25×/2× premium）與 server tool 按次計費。

## WP2 — Gemini facts pack 檢索層

- [x] **T013** 新增 `backend/ai/retrieval.py`：`FactsPack` / `FactEvent` + `fetch_facts()`；
      用 flash 而非 pro；取**完整** grounding URL 清單（非 `_grounding_sources` 的前 4 筆截斷版）；
      無 URL 的 event 丟棄。
- [x] **T014** 刪除死碼 `gemini_client.fetch_web_context()`；`FULL_BRIEF_RULES` 中懸空的
      `features.web_context` 契約改指向 facts pack。**不**把 facts 塞回 `features`——
      那會讓同一份線索在 prompt 裡出現兩次（features JSON 一次 + facts pack 一次），
      稽核副本改走 `report["facts"]`。

## WP3 — 晨報決策層切換

- [x] **T015** `backend/ai/schemas.py`：新增 `WatchItemDraft` / `BriefDraft` / `FactCheck`
      （排除 `risk_score`/`conviction_score`/`size_weight`）。
- [x] **T016** `backend/ai/prompts.py`：新增 `build_decision_prompt()` 含**查證契約**；
      features JSON 改 `sort_keys=True` 且去除 `indent=2`。
- [x] **T017** `backend/reports/morning_brief.py`：facts → 單次 Claude 決策；刪除第②段格式化；
      Gemini 降級路徑 + `decision_provider` 欄位；`facts` / `fact_checks` 落地 JSON。
- [x] **T018** `/brief/latest` 的 `cost` 加 `month_remaining_twd`。

## WP4 — /ask

- [x] **T019** `backend/api/ask.py` 走 `DecisionLLM.answer_question`；`max_uses` 4/2；
      `cache_control` 依 `enable_claude_prompt_cache`（預設關）；意圖分類器維持 Gemini flash-lite。

## WP6 — 測試

- [x] **T020** `tests/test_claude_client.py`：payload 無 sampling 參數、refusal 降級、
      pause_turn 收斂、tools 逐字穩定、web_fetch 未開 citations。
- [x] **T021** `tests/test_cost_tracker.py` 擴充：Anthropic 費率、cache write premium、
      回歸釘住「`claude-opus-5` 不落 flash 費率」。
- [x] **T022** `tests/test_facts_pack.py`：URL 補齊、無來源丟棄。
- [x] **T023** `tests/test_provider_switch.py`：`LLM_DECISION_PROVIDER=gemini` 舊路徑可用。
- [x] **T024** `tests/test_ask_cache_flag.py`：預設不得出現 `cache_control`。
- [x] **T025** `pytest tests/ -q` 全綠（既有 `test_gemini_retry` / `test_score_fusion` 不得回歸）。

## 部署

- [ ] **T026** `.env` 補 `ANTHROPIC_API_KEY`（勿 commit）→ `docker compose up -d --build backend`。
- [ ] **T027** 實測：`count_tokens` 量真實 features + 連跑 3 天晨報看 `usage` 與
      `fact_checks` 分布，把 `max_uses` 從拍腦袋值收斂成實測值。
