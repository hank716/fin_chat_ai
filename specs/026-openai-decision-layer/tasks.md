# Tasks: 決策層新增 OpenAI 供應商（方案 B）

**Input**: Design documents from `specs/026-openai-decision-layer/`

**Prerequisites**: plan.md、spec.md、research.md、data-model.md、contracts/decision-llm.md、contracts/provider-compare-cli.md、quickstart.md

**Tests**: 需要。spec SC-001 要求 OpenAI 路徑全部有自動化測試（無 API key、全 mock），且既有基線 543 passed 維持通過、期望值不得改（SC-002）。

**測試執行**：`docker compose exec -T backend pytest -q`（新依賴加入後需先 `docker compose build backend`）。

**Organization**: US1–US3 同為 P1，但彼此有技術依賴：US1 的晨報要靠 US3 記帳、靠 US2 查證，
所以實作順序依 plan.md 為 **US3 → US2 → US1 → US4 → US5**；標籤仍對應 spec 的使用者故事。

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 可平行（不同檔案、不依賴未完成任務）
- **[Story]**: 對應 spec.md 的 US1–US5

---

## Phase 1: Setup

**Purpose**: 依賴與設定欄位

- [X] T001 在 `backend/requirements.txt` 新增 `openai>=3.35,<3.36` 與 `beautifulsoup4>=4.12,<5`（後者容器已有，補明確 pin），`docker compose build backend` 確認可裝
- [X] T002 在 `backend/config.py` 新增 data-model.md §1 的全部欄位：`openai_api_key`（預設 `""`）、`openai_model_decision`、`openai_model_chat`（預設 `gpt-6.1-sol`）、`openai_brief_effort`、`openai_effort`（預設 `medium`）、`openai_max_output_tokens=32000`、`openai_brief_fetch_uses=3`、`openai_brief_search_uses=2`、`openai_chat_search_uses=2`、`openai_fetch_max_chars=12000`、`openai_max_tool_rounds=8`、`openai_shadow_until: date | None = None`、`openai_shadow_cost_estimate_twd=15.0`；`llm_decision_provider` 註解值域改為 anthropic / openai / gemini
- [X] T003 [P] 在 `.env.example` 加上述 `OPENAI_*` 變數（全部註解掉或留空、**不可寫行內 `#` 註解**），註明 key 未設時系統仍可啟動

---

## Phase 2: Foundational（阻擋所有 user story）

**Purpose**: 把 `FetchAttempt` 搬到中立模組，讓 openai 路徑不必 import claude_client

- [X] T004 建立 `backend/ai/fetch_tool.py`，把 `backend/ai/claude_client.py:205` 的 `FetchAttempt` dataclass 原樣搬過來，並新增兩個選填欄位 `reject_reason: str | None = None`、`final_url: str | None = None`（data-model.md §2）；此時檔案只放 dataclass，`FetchSession` 留給 US2
- [X] T005 `backend/ai/claude_client.py` 改為 `from ai.fetch_tool import FetchAttempt` 並保留同名 re-export（`__all__` 或模組層名稱），其他 import `claude_client.FetchAttempt` 的地方（grep `FetchAttempt`）不需改
- [X] T006 跑全部測試，確認 543 passed 零回歸（尤其 `tests/test_claude_client.py`、`tests/test_morning_brief_pipeline.py`、`tests/test_verification_stats.py`）；報告 JSON 序列化 `FetchAttempt` 時新欄位為 `None` 不得改變既有測試期望值

**Checkpoint**: 基礎就緒，US3 / US2 可平行開始

---

## Phase 3: User Story 3 - 成本可歸因且受預算約束 (Priority: P1)

**Goal**: OpenAI 花費以正確牌價計入、記入 `openai` 桶，月／日上限與節儉模式照常生效

**Independent Test**: 固定 mock usage（含 cached、cache write、reasoning、搜尋次數）算出成本與手算值誤差 0；`by_provider()` 回傳含 `openai`

### Tests for User Story 3

- [X] T007 [P] [US3] 新增 `tests/test_cost_openai.py`：(a) spec US3 情境 1——input 60k（cached 40k、write 0）、output 18k（reasoning 10k）、搜尋 2 次，以 gpt-6.1-sol 牌價手算比對（cached 不重複計、reasoning 不另計）；(b) cached 與 cache_write 互斥：input 60k、cached 30k、write 20k → 未命中 10k；(c) input > 272k 套長文級距；(d) 未知 `gpt-*` 以 gpt-6-astra 估並 `caplog` 斷言 warning；(e) `provider_of("gpt-6.1-sol")=="openai"`、`provider_of("claude-opus-5")=="anthropic"`、`provider_of("gemini-flash-latest")=="gemini"`；(f) `record_cost(..., provider="openai")` 後 `by_provider()` 含 openai 鍵（沿用 `tests/test_cost_by_provider.py` 的 fakeredis／monkeypatch 慣例）

### Implementation for User Story 3

- [X] T008 [US3] 在 `backend/cost/tracker.py` 新增 `_PRICING_OPENAI`（research.md R6 六個模型，USD/MTok 的 in / cached / write / out，上方註解「核對日期 2026-10-19，gpt-5.6-sol 優惠至 2026-11-21」）、`_OPENAI_DEFAULT = gpt-6-astra`、`_TIER_THRESHOLD_OPENAI = 272_000`、`_OPENAI_WEB_SEARCH_USD = 0.01`
- [X] T009 [US3] 在 `backend/cost/tracker.py` 的 `cost_of_usage` 加第三條分支（model 以 `gpt-` 或 `o` 系列開頭）：`billable_uncached = max(input - cached - cache_write, 0)`，依 data-model.md §4 公式計 USD×32；未知模型 `logger.warning`；Anthropic 與 Gemini 分支一行都不動
- [X] T010 [US3] `backend/cost/tracker.py`：`PROVIDERS` 擴為 `("gemini", "anthropic", "openai")`；`provider_of()` 加 `gpt-` / `o\d` 前綴 → `"openai"`；確認 `brief_should_degrade()`、日／月上限讀的是總桶（不需改，只加註解說明 openai 已含在內）
- [X] T011 [US3] 跑 `tests/test_cost_openai.py` 與既有 `tests/test_cost_tracker.py`、`tests/test_cost_by_provider.py`、`tests/test_cost_budget_gate.py` 全綠

**Checkpoint**: 計價可用，後續所有 OpenAI 呼叫都能正確記帳

---

## Phase 4: User Story 2 - 查證閉環在 OpenAI 下仍可稽核 (Priority: P1)

**Goal**: 本地 `web_fetch` 工具只能開 facts pack 白名單網址，擋私有位址，每次請求都由系統端記錄

**Independent Test**: 以 `httpx.MockTransport` + monkeypatch `socket.getaddrinfo`，驗證白名單內成功、白名單外與私有位址被拒且不發連線、額度用盡、重複 URL 不扣額度

### Tests for User Story 2

- [X] T012 [P] [US2] 新增 `tests/test_fetch_tool.py`，涵蓋 contracts/decision-llm.md §3 的不變式：(a) 白名單內 HTML → `ok=True`、script/style/nav/footer 被去除、`content_chars` 為截斷前長度、回傳 JSON 含 `truncated`；(b) 白名單外 → `url_not_allowed` + `reject_reason="not_in_facts_pack"`，**MockTransport 收到 0 個請求**；(c) host 解析到 127.0.0.1 / 10.x / 169.254.x / ::1 → `url_not_allowed` + `private_address`；(d) 白名單內網址 302 轉到 127.0.0.1 → 被拒（每跳重檢）；(e) 302 轉到白名單外公開網址 → 允許並記 `final_url`；(f) 超過 5 跳 → `url_not_accessible`；(g) `ftp://` → `url_not_allowed` + `bad_scheme`；(h) 第 N+1 次 → `max_uses_exceeded`；(i) 同一 URL 第二次 → 回快取內容、不新增 attempt、不扣額度；(j) HTTP 404 → `url_not_accessible`；逾時 → `unavailable`；`application/pdf` → `unsupported_content_type` 且不回內容；(k) 超過 2 MB 只讀前 2 MB；(l) 白名單正規化：大小寫 host、fragment、尾 `/` 視為同一網址，但不做前綴匹配；(m) 任何例外都不 raise
- [X] T013 [P] [US2] 在 `tests/test_verification_stats.py` 新增案例：以 `FetchSession` 產生的五種 error_code attempts 餵 `classify_outcomes`，斷言全部落在既有分類（`_BUDGET_CODES` / `_UNREACHABLE_CODES` / `_TRANSIENT_CODES`），分類邏輯不需修改

### Implementation for User Story 2

- [X] T014 [US2] 在 `backend/ai/fetch_tool.py` 實作 `normalize_url()` 與 `FetchSession.__init__(allowed_urls, *, max_uses, max_chars)`、`attempts` property（research.md R3 白名單比對規則）
- [X] T015 [US2] 在 `backend/ai/fetch_tool.py` 實作 SSRF 檢查 `_is_public_host(host)`：`socket.getaddrinfo` 解析所有 IP，任一屬 `ipaddress` 的 private / loopback / link_local / reserved / multicast / unspecified 即拒；只允許 http/https
- [X] T016 [US2] 在 `backend/ai/fetch_tool.py` 實作 `FetchSession.fetch(url) -> tuple[str, FetchAttempt | None]`：白名單 → 額度 → 重複快取 → `httpx.Client(follow_redirects=False, timeout=10)` 手動跟轉址（≤5 跳、每跳重檢 scheme 與 IP）→ 串流讀取上限 2 MB → content-type 分流（`text/html` 用 BeautifulSoup `html.parser` 去雜訊取純文字、`text/plain` 原樣、其他 `unsupported_content_type`）→ 截斷 `max_chars` → 依 contracts/decision-llm.md §2 格式回 JSON 字串；所有例外轉失敗 attempt、永不 raise
- [X] T017 [US2] `backend/reports/morning_brief.py:410` 的 `fetch_limit` 改為依 provider 取：anthropic → `settings.claude_brief_fetch_uses`、openai → `settings.openai_brief_fetch_uses`（`_verification_stats` 需多收一個 `provider` 參數）；`:551` 的 `verification_active` 改為 `not frugal and provider in ("anthropic", "openai")`
- [X] T018 [US2] 跑 `tests/test_fetch_tool.py`、`tests/test_verification_stats.py`、`tests/test_morning_brief_pipeline.py` 全綠

**Checkpoint**: 查證工具可獨立使用，等待 US1 接上模型

---

## Phase 5: User Story 1 - 用 OpenAI 產出一份完整晨報 (Priority: P1) 🎯 MVP

**Goal**: `LLM_DECISION_PROVIDER=openai` 時晨報照常產出，結構、guardrail、查證欄位與 Claude 相同；失敗退回 Gemini 降級

**Independent Test**: 無 key、以假 OpenAI client 回 `SimpleNamespace` response 跑完整晨報管線，報告通過 schema 與 guardrail，`decision_provider=openai`、成本記入 openai 桶；預設設定下行為零差異

### Tests for User Story 1

- [X] T019 [P] [US1] 新增 `tests/test_openai_client.py`（monkeypatch `openai_client._client()`，response 用 `SimpleNamespace`，慣例同 `tests/test_claude_client.py`）：(a) `schema_of(BriefDraft)` 每個 object 的 `required == properties.keys()` 且 `additionalProperties is False`，非必填欄位可為 null；(b) 模型回 null 的選填欄位在驗證前被剝除，`BriefDraft.model_validate` 成功；(c) 工具迴圈：第 1 輪回 `function_call(web_fetch)` → 第 2 輪 input 含上一輪整段 output（含 reasoning item）+ `function_call_output` → 第 3 輪回最終 JSON；(d) 超過 `openai_max_tool_rounds` → `LLMError` 且 `exc.fetch_attempts` 帶已發生的查證；(e) `status="incomplete"`、reason `max_output_tokens` → `LLMError`；(f) refusal content → `LLMRefused`；(g) JSON 不合法 → `LLMError`；(h) R8 錯誤映射表逐條（RateLimitError→LLMQuotaExceeded、BadRequestError→LLMBadRequest、APIConnectionError/APITimeoutError/5xx→LLMUnavailable）；(i) usage 多輪加總為 data-model.md §3 的鍵，`web_search_requests` 由 `web_search_call` items 計數、`rounds` 正確；(j) 請求參數：`store=False`、`include=["reasoning.encrypted_content"]`、`parallel_tool_calls=False`、tools 順序 web_fetch 在前、`reasoning.effort` 與 `max_output_tokens` 取自設定；(k) key 為空 → `LLMError("OPENAI_API_KEY 未設定")`
- [X] T020 [P] [US1] 在 `tests/test_provider_switch.py` 加 openai 案例：(a) `get_decision_llm("openai")` 回 `OpenAIDecisionLLM`；(b) 未知名稱錯誤訊息列出 anthropic / openai / gemini；(c) `draft_brief(frugal=True)` → tools 為空、facts 傳 `"[]"`、effort=low；(d) `frugal=False` 的 system / user prompt 與 Anthropic 實作的字串**完全相同**；(e) 白名單＝facts_json 中所有 `url`
- [X] T021 [P] [US1] 在 `tests/test_morning_brief_pipeline.py` 加 openai 端到端案例（mock OpenAI client + mock Gemini 召回）：(a) 成功 → `decision_provider=="openai"`、`decision_model`、`decision_model_version`、`cost.tokens.reasoning_tokens` 存在，`fact_checks` 與 `verification` 有值，`record_cost` 以 provider="openai" 被呼叫；(b) OpenAI raise `LLMUnavailable` → 走 Gemini 降級、報告標示降級、已發生的 fetch attempts 仍記錄；(c) key 未設 + provider=openai → `gemini-fallback` 並在 markdown 可見原因；(d) 節儉模式 + openai → 無查證、無搜尋、`verification_active=False`；(e) 預設 provider（anthropic）既有案例期望值不變

### Implementation for User Story 1

- [X] T022 [US1] 新增 `backend/ai/openai_client.py` 基礎：`_client()` singleton（`OpenAI(api_key=..., max_retries=..., timeout=900)`，key 空時 raise `LLMError`）、`_map_error(exc)`（research.md R8）、`schema_of(model_cls)`（R2 strict 化，保留 `$defs`/`$ref`）、`_strip_nulls(data, schema)`
- [X] T023 [US1] 在 `backend/ai/openai_client.py` 實作 `_usage_of(response)` 與多輪加總（data-model.md §3；讀欄位一律 `getattr(..., 0)` 容錯）、回應 `model` 版本擷取
- [X] T024 [US1] 在 `backend/ai/openai_client.py` 實作 `generate_structured(system, user, schema_cls, *, tools, fetch_session, effort, max_output_tokens)`：工具迴圈（R4）——`function_call` 名為 `web_fetch` 交給 `fetch_session.fetch`、整段 `response.output` 接回 input、輪數上限、incomplete / refusal / JSON 失敗處理，失敗時用 `_with_attempts` 同等機制把 `fetch_session.attempts` 掛到例外上；回 `(parsed, usage, attempts, model_version)`
- [X] T025 [US1] 在 `backend/ai/openai_client.py` 實作 `generate_text(system, user, *, tools, effort)` 供 /ask 使用（只允許 hosted `web_search`，`max_tool_calls=openai_chat_search_uses`）
- [X] T026 [US1] 在 `backend/ai/llm_client.py` 新增 `OpenAIDecisionLLM`（`name="openai"`）：`draft_brief` 依 contracts/decision-llm.md §1——從 `facts_json` 取 url 建 `FetchSession(max_uses=openai_brief_fetch_uses, max_chars=openai_fetch_max_chars)`、tools=[web_fetch function（contract §2 JSON 原樣）, `{"type":"web_search"}`]、prompt 用 `prompts.build_decision_system(verify=not frugal)` / `build_decision_prompt(...)`、frugal 時 tools=[]、facts=`"[]"`、effort=low；`answer_question` 委派 `generate_text`
- [X] T027 [US1] `backend/ai/llm_client.py:170-176` 的 `get_decision_llm` 加 `"openai"` 分支，未知值錯誤訊息改為「可用：anthropic / openai / gemini」
- [X] T028 [US1] `backend/reports/morning_brief.py:523` 的 `decision_model` 依 provider 取 `settings.openai_model_decision`；`cost_info` 新增 `decision_model`、`decision_model_version`、`tokens.reasoning_tokens`（Claude 路徑填 0）；key 未設時降級原因寫入報告與 log（不靜默）
- [X] T029 [US1] 確認 `backend/reports/markdown_builder.py`、`backend/reports/discord_summary.py`、`backend/reports/web_renderer.py` 顯示 provider 的地方能正確呈現 `openai`（若有寫死 `anthropic`/`Claude` 字樣的判斷就改成通用，不新增供應商分支）
- [X] T030 [US1] 跑 `tests/test_openai_client.py`、`tests/test_provider_switch.py`、`tests/test_morning_brief_pipeline.py` 與全套測試，基線不退

**Checkpoint**: MVP——切 `LLM_DECISION_PROVIDER=openai` 即可出晨報（拿到 key 後即可真跑）

---

## Phase 6: User Story 4 - 對照實驗與切換決策 (Priority: P2)

**Goal**: 同日影子模式累積配對資料；比較指令輸出並排指標與三條切換門檻

**Independent Test**: 以既有 Claude 報告 + mock 產生的影子報告跑 `provider_compare`，輸出兩欄指標表；影子不出現在 `list_reports()`

### Tests for User Story 4

- [X] T031 [P] [US4] 新增 `tests/test_shadow_brief.py`：(a) `openai_shadow_until >= 今日`、非節儉 → 主報告存檔並發布**之後**才呼叫影子（斷言呼叫順序）、寫入 `REPORTS_DIR/shadow/<report_id>_openai.json` 且欄位符合 data-model.md §5；(b) 影子共用主晨報的 `feats` / `facts`，召回層只被呼叫一次；(c) `list_reports()`、`latest_report_id()`、`report_date_exists()` 撿不到影子；(d) `month_remaining() - openai_shadow_cost_estimate_twd < brief_degrade_reserve_twd` → 跳過、主報告 `shadow.status=="skipped"`、reason=budget；(e) 影子 raise 任意例外 → 主報告不受影響、影子檔寫 `error`、`shadow.status=="error"`；(f) `openai_shadow_until` 已過或為 None → 不執行；(g) 主晨報為節儉模式 → 不執行；(h) 影子成本以 provider="openai" 記帳；(i) Discord 推送與網頁發布函式的呼叫參數中不含影子內容（SC-007）
- [X] T032 [P] [US4] 新增 `tests/test_provider_compare.py`：(a) 只配對主報告與影子都存在的日子；(b) 影子 `error` 非空的配對計入「影子失敗 N 次」不進平均；(c) 影子成本＝影子 decision_twd + 主報告 retrieval_twd；(d) 三條門檻（成本 ≤ 60%、裁決率 ≥ 80%、違規不多於 Claude）的成立／不成立各一案；(e) 路徑不存在或兩邊 0 篇 → 非零 exit 且不印空表；(f) `--since/--until` 過濾；(g) `--json` 輸出可 `json.loads`

### Implementation for User Story 4

- [X] T033 [US4] 新增 `backend/reports/shadow_brief.py`：`should_run_shadow(today, frugal) -> tuple[bool, str | None]`（到期、節儉、預算預留三道檢查）與 `run_shadow(feats, facts, calibration_text, report_id, report_date, main_retrieval_twd) -> dict`——呼叫 `get_decision_llm("openai").draft_brief`、套 `_backfill_caution` + `run_guardrails` + `_verification_stats(provider="openai")`、`record_cost(provider="openai")`、寫 `REPORTS_DIR/shadow/<report_id>_openai.json`（建目錄）；OpenAI 失敗時**不跑 Gemini 降級**（影子不發布，降級版沒有比較價值又多花錢），只把錯誤寫入影子檔 `error` 欄位、已發生的 fetch attempts 照樣落地（spec US4 情境 3）；整段 try/except、回 `{"status","reason","path"}`
- [X] T034 [US4] `backend/reports/morning_brief.py` 的 `generate_morning_brief`：在存檔與發布（Discord／網頁／Supabase）全部完成**之後**加一個呼叫點 `shadow_brief.run_shadow(...)`，把回傳寫入主報告 `shadow` 欄位並覆寫主報告 JSON；呼叫本身包 try/except，失敗只 log
- [X] T035 [US4] 新增 `backend/reports/provider_compare.py`：argparse（`REPORTS_DIR` 位置參數、`--since`、`--until`、`--json`）、配對邏輯、指標計算（裁決率呼叫 `reports.verification_stats` 既有彙總函式，不重寫分類）、contracts/provider-compare-cli.md 的文字表格與門檻輸出、錯誤路徑非零 exit
- [X] T036 [US4] 跑 `tests/test_shadow_brief.py`、`tests/test_provider_compare.py` 與全套測試

**Checkpoint**: 拿到 key 後設 `OPENAI_SHADOW_UNTIL` 即可開始 5–10 天對照

---

## Phase 7: User Story 5 - /ask 問答可走 OpenAI (Priority: P3)

**Goal**: 問答作答模型可設為 OpenAI，意圖分類仍走 Gemini

**Independent Test**: mock OpenAI 呼叫 /ask，回答正常、成本記入 openai、日上限滿時照常拒絕

### Tests for User Story 5

- [X] T037 [P] [US5] 在 `tests/test_ask_endpoint.py` 加 openai 案例：(a) provider=openai → 以 `openai_model_chat` 作答、`model_used` 正確、`record_cost(provider="openai")`；(b) tools 只有 `web_search`、無 `web_fetch`；(c) 日上限已滿 → 照現行規則拒絕；(d) 意圖分類仍呼叫 Gemini；(e) 預設 anthropic 既有案例期望值不變

### Implementation for User Story 5

- [X] T038 [US5] `backend/api/ask.py:169` 的分支改為 `llm.name in ("anthropic", "openai")`，`model_used` 依 provider 取 `settings.claude_model_chat` / `settings.openai_model_chat`；effort 取 `openai_effort`
- [X] T039 [US5] 跑 `tests/test_ask_endpoint.py`、`tests/test_ask_cache_flag.py` 與全套測試

---

## Phase 8: Polish & Governance

- [X] T040 `.specify/memory/constitution.md` 升 2.1.0 → **2.2.0**（MINOR）：Principle I 決策層可選供應商列入 OpenAI，並補「不發布的同日影子對照」允許條款；Principle II 補試驗期月上限 NT$1,000 須有明確結束日、到期恢復 NT$800、影子花費計入總額且不得使主晨報進入節儉模式；更新 Sync Impact Report
- [X] T041 [P] 同步 `README.md` 與 `ARCHITECTURE.md` 的供應商描述（決策層 anthropic / openai / gemini、影子模式、本地 web_fetch 白名單閉環）
- [X] T042 [P] `.env.example` 補試驗期範例註解（`OPENAI_SHADOW_UNTIL`、`MONTHLY_COST_LIMIT_TWD=1000` 只在試驗期使用），與憲章、`backend/config.py`、README 四處一致
- [X] T043 依 quickstart.md §A 冒煙（2026-10-22：無 key + provider=openai + 影子留空 → 啟動正常、/health 200、分派到 openai；「觸發真晨報得到 gemini-fallback」未實跑以免真實花費與推送，改由 test_morning_brief_pipeline 的 openai 降級測試覆蓋）：`docker compose build backend && docker compose up -d backend`、全套 pytest（≥543 + 新增全綠）、`LLM_DECISION_PROVIDER=openai` 且無 key 時 `/health` 200、觸發晨報得到 `gemini-fallback` 報告且 markdown 可見降級提示；驗完改回 anthropic
- [X] T044 grep 確認 `backend/` 中除 `ai/openai_client.py` 外沒有任何地方 import `openai` 套件型別（FR-014：上層只認中立例外）
- [X] T045 更新記憶 `openai-migration-eval.md`：026 實作完成、等 key 到手後依 quickstart §B 開始對照

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)** → **Foundational (Phase 2)** → 其餘
- **US3 (Phase 3)** 與 **US2 (Phase 4)**：Foundational 完成後可平行（不同檔案：`cost/tracker.py` vs `ai/fetch_tool.py`）；T017 改 `morning_brief.py`，與 US1 的 T028 同檔需依序
- **US1 (Phase 5)**：依賴 US3（記帳）與 US2（`FetchSession`）
- **US4 (Phase 6)**：依賴 US1（`get_decision_llm("openai")`）
- **US5 (Phase 7)**：依賴 US1 的 T025 / T026（`generate_text`、`OpenAIDecisionLLM.answer_question`）與 US3；與 US4 互相獨立，可平行
- **Polish (Phase 8)**：T040–T042 可隨時做；T043–T045 在全部故事完成後

### Within Each User Story

- 測試先寫並確認失敗，再實作
- `openai_client.py` 內：T022 → T023 → T024 → T025（同檔依序）
- `morning_brief.py` 內：T017 → T028 → T034（同檔依序）

### Parallel Opportunities

- T003 與 T001/T002 平行
- Phase 3 與 Phase 4 整段平行；T007、T012、T013 測試檔可同時寫
- Phase 5 的三個測試任務 T019、T020、T021 可同時寫
- Phase 6 的 T031、T032 可同時寫；T035（provider_compare）與 T033/T034（影子）不同檔可平行
- Phase 6 與 Phase 7 整段平行
- T041、T042 平行

---

## Parallel Example: User Story 1

```text
# 同時寫三個測試檔：
T019 tests/test_openai_client.py
T020 tests/test_provider_switch.py（openai 案例）
T021 tests/test_morning_brief_pipeline.py（openai 端到端）

# 之後依序：T022 → T023 → T024 → T025（openai_client.py）→ T026 → T027（llm_client.py）→ T028（morning_brief.py）
```

---

## Implementation Strategy

### MVP（Phase 1–5）

1. Setup + Foundational（T001–T006），確認零回歸
2. US3 計價 + US2 查證工具（可平行）
3. US1 接上 OpenAI 決策層
4. **停下驗證**：無 key 下全套測試綠、`provider=openai` 無 key 冒煙走降級
5. 此時已可手動切換供應商真跑（拿到 key 後）

### Incremental Delivery

1. MVP 完成 → commit `feat(026): OpenAI 決策層 + 本地 web_fetch 白名單 + 計價`
2. US4 影子 + 比較 CLI → commit，拿到 key 後即可開對照
3. US5 /ask → commit
4. 治理（憲章 2.2.0、四處同步）→ commit；rebuild 部署

### 不在本 spec 範圍

- 真實 5–10 天對照與切換決定（quickstart §B，取得 key 後才進行，不阻擋 spec 完成）
- 移除 Claude 實作、召回層換供應商、/ask 意圖分類換供應商

---

## Notes

- 全程無 API key；所有 OpenAI 呼叫經 `openai_client._client()`，測試一律 monkeypatch
- 預設 `LLM_DECISION_PROVIDER=anthropic` 下任何既有測試期望值都不得修改（SC-002）
- `.env` 不可寫行內 `#` 註解
- commit 訊息格式 `feat(026): ...`
