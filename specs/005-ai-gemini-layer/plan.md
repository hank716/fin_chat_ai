# Implementation Plan: AI 層 — Gemini 結構化生成、grounding、快取

**Branch**: `005-ai-gemini-layer` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

以 httpx 直呼 Gemini v1beta generateContent（`X-goog-api-key`），強制 responseSchema 產結構化 JSON、
parse 成 pydantic 模型；晨報走兩段式（PRO+Google 搜尋研究 → Flash 格式化）突破 schema 與 tool 不能
並用的限制；問答前以 Flash-Lite 意圖分類（fail-open）、當日靜態 context 走明確快取（優雅降級）省 token；
HTTP 狀態以 tenacity + 自訂例外分流（503 重試 / 429·400 fail-fast）。系統唯一 LLM 為 Gemini。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: httpx、tenacity、pydantic；redis（快取名稱）；`activity.monitor`（流量標記）
**Storage**: redis（`gemini:cache:*` 名稱）；Gemini cachedContents（雲端）
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器）
**Project Type**: web-service 內部模組（LLM 呼叫原語層）
**Performance Goals**: 晨報單篇約 NT$8；問答以快取/分類降 token
**Constraints**: 不輸出 raw CoT；引用快取時不得重送 tools；分類器/快取皆不得成單點故障
**Scale/Scope**: `backend/ai/*.py`（gemini_client 264 / prompts 229 / schemas 212 / cache 89 / llm_client 31）

## Constitution Check

*GATE: 對照憲章七原則。*

- **I. Gemini-only** — ✅ 唯一 LLM；`llm_client` 薄 Protocol 但明確不接 Claude API（ARCHITECTURE §4.1）。
- **II. 成本紀律** — ✅ 意圖分類（Flash-Lite）+ 明確快取 + 兩段式（貴模型只在研究段）降 token；
  usage 口徑對齊 [009-cost-control]。
- **III. Fail-Closed / 不輸出 raw CoT** — ✅ 只輸出結構化結論；輸出交由 [004-guardrails] 二次驗證。
- **IV. Point-in-Time** — ✅ 僅消費 features 既有資料（研究段連網取即時新聞，非回填歷史）。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。

**已知取捨（非違規）**：`*-latest` 別名版本與 009 費率表需人工同步；明確快取結構改變須 bump `_KEY_VERSION`。
**結論**：通過。

## Project Structure

### Documentation (this feature)

```text
specs/005-ai-gemini-layer/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/ai/
├── gemini_client.py   # _generate_json / generate_text / classify_finance_intent /
│                      # analyze_full_brief(_grounded) / _usage_of / 例外分流 / SEARCH_TOOLS
├── gemini_cache.py    # get_or_create_qa_cache（cachedContents，含 tools，優雅降級）
├── llm_client.py      # LLMClient Protocol + GeminiClient（不接 Claude）
├── prompts.py         # build_* prompt 組裝（static/variable 分塊配合快取）
└── schemas.py         # pydantic 模型 + GEMINI_*_SCHEMA（responseSchema）
backend/config.py      # gemini_model_*（brief/qa/classifier）、enable_intent_filter、
                       # enable_gemini_explicit_cache、gemini_cache_ttl_seconds（.env 可覆寫）
tests/ai/              # ⬜ 待新增：pytest 覆蓋（目前不存在）
```

**Structure Decision**: 保留「薄抽象 + 函式化」設計：`llm_client` 只做 Protocol 佔位（憲章 I 明確單一
供應商），實際邏輯集中在 `gemini_client`；快取與 prompts 拆檔以維持可測與可調。

## Complexity Tracking

> 無 Constitution 違規，免填。
