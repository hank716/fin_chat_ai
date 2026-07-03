# Tasks: AI 層 — Gemini 結構化生成、grounding、快取

**Feature**: `005-ai-gemini-layer` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 結構化生成核心（基線，已實作）

- [X] T001 `_generate_json`：responseMimeType/responseSchema + parse + 空/非 JSON 檢查 `backend/ai/gemini_client.py`（FR-001）
- [X] T002 HTTP 狀態分流例外：503/429/400 → `GeminiUnavailable/QuotaExceeded/BadRequest`（FR-003）
- [X] T003 tenacity 重試（僅 RequestError/Unavailable，指數退避）
- [X] T004 `_usage_of`：input/output(含 thinking)/cached/tool 四欄（FR-008）
- [X] T005 pydantic 模型 + `GEMINI_*_SCHEMA` `backend/ai/schemas.py`（FR-001, FR-009）
- [X] T006 `analyze_full_brief` / `analyze_intermarket`（單段結構化）

## Phase 2 — grounding / 快取 / 分類（基線，已實作）

- [X] T007 `analyze_full_brief_grounded`：兩段式 PRO+search → Flash 格式化，回兩段 usage（FR-002）
- [X] T008 `generate_text(use_search, cached_content)`：附來源、帶明確快取時不重送 tools（FR-007）
- [X] T009 `classify_finance_intent`：Flash-Lite + fail-open（FR-005）
- [X] T010 `get_or_create_qa_cache`：cachedContents（含 tools）+ key 版本化 + 優雅降級（FR-006）
- [X] T011 `llm_client` Protocol + GeminiClient（明確不接 Claude）（FR-004）
- [X] T012 模型/開關設定化（brief/qa/classifier、intent_filter、explicit_cache、cache_ttl）（FR-010）

## Phase 3 — 測試基線（未竟，高優先）

> 以 `respx`/httpx mock 打 Gemini 端點；redis 以 fakeredis/monkeypatch。

- [ ] T013 建立 `tests/ai/`（httpx mock transport + fixtures）
- [ ] T014 [P] test：`_generate_json` 合法 JSON → dict+usage；非 JSON/空/no candidates → GeminiError（US1）
- [ ] T015 [P] test：503→Unavailable 重試、429→QuotaExceeded、400→BadRequest 不重試（US1 / FR-003）
- [ ] T016 [P] test：`_usage_of` 含 thoughtsTokenCount、cached/tool 分離（FR-008 / SC-003）
- [ ] T017 [P] test：`classify_finance_intent` HTTP/解析失敗 → (True, {})（US3 / FR-005）
- [ ] T018 [P] test：`get_or_create_qa_cache` 400/redis 故障 → None；成功寫 redis 且 TTL 早於 cachedContents（US3 / FR-006）
- [ ] T019 test：`analyze_full_brief_grounded` 依序呼叫研究段(search)+格式化段，回兩段 usage（US2 / FR-002）
- [ ] T020 test：`GEMINI_API_KEY` 未設 → GeminiError（edge）

## Phase 4 — 後續強化（backlog，選用）

- [ ] T021 `*-latest` 別名版本與 [009-cost-control] 費率表的一致性檢查（避免低估）
- [ ] T022 明確快取命中率/降級率的觀測指標
- [ ] T023 `/speckit-converge` 掃描 ai/ 與本 spec 落差、補未列任務

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 結構化 JSON | T001–T006, T014–T016, T020 | 程式✅ / 測試⬜ |
| US2 兩段式 grounded | T007–T008, T019 | 程式✅ / 測試⬜ |
| US3 分類 + 明確快取 | T009–T010, T017–T018 | 程式✅ / 測試⬜ |
