# Tasks: 輸出護欄 — Symbol Guard 與六道驗證

**Feature**: `004-guardrails-symbol-guard` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**慣例**: `[X]` = 已完成（基線，現況程式已實作）；`[ ]` = 未竟待辦。`[P]` = 可平行。

## Phase 1 — 核心護欄（基線，已實作）

- [X] T001 定義 `run_guardrails(result, features) -> (BriefResult, report)` 純函式框架 `backend/guardrails/verify.py`
- [X] T002 Source/Metric Guard：解析 `evidence.source_ref`，`_MISSING` 則移除並記 error（FR-001）
- [X] T003 `_resolve` 支援 JSONPath 方言（省略根前綴、字串鍵/數字索引 bracket、尾註解截斷）（FR-009）
- [X] T004 Symbol Guard：候選 symbol ∈ `features.tw.stocks` 校驗（FR-002）
- [X] T005 **Symbol Guard fail-closed**：stocks 空時移除全部候選、理由標「資料範圍為空」（FR-003, commit `7e7fd1a`）
- [X] T006 News Citation Guard：url/title 比對 + source/date/title 檢查 + url/tier/provider 回填（FR-004）
- [X] T007 Advice / Causality Guard：掃描 `ADVICE_BANNED`/`CAUSALITY_BANNED` 記 warning（FR-005）
- [X] T008 Data Age Guard：缺 `data_as_of` 記 error（FR-006）
- [X] T009 組裝 report（passed/counts/error_count/warning_count/violations）+ `model_copy` 保護輸入（FR-007, FR-008）
- [X] T010 於 `backend/reports/morning_brief.py:348` 接線至產報流程

## Phase 2 — 測試基線（未竟，高優先）

> 憲章要求高風險模組補 pytest 作為驗收依據；目前專案無 `tests/`，本 feature 為第一批。

- [ ] T011 建立 `tests/` 與 `tests/conftest.py`（features/BriefResult fixtures 工廠）
- [ ] T012 [P] test：捏造 source_ref 之 evidence 被移除 + `metric` error（US1 / SC-001）
- [ ] T013 [P] test：候選 symbol 不在 stocks 被移除（US1）
- [ ] T014 [P] test：**stocks 為空 → 全部候選移除**（US2 / SC-002，fail-closed 回歸守門）
- [ ] T015 [P] test：news 無法比對被移除；比對成功缺 url 則回填不丟（US1 / edge case）
- [ ] T016 [P] test：`_resolve` 方言——`$.tw.stocks["2408"]`、尾註解截斷、數字索引（FR-009）
- [ ] T017 [P] test：禁語標 warning 但不刪內容；缺 data_as_of 記 error（US3）
- [ ] T018 test：輸入物件不被 mutate（FR-008）；相同輸入 report 可重現（SC-003）

## Phase 3 — 後續強化（backlog，選用）

- [ ] T019 將 guardrail report 的攔截統計顯示於 Web Report 頁面（若尚未呈現）
- [ ] T020 以 `/speckit-converge` 掃描 verify.py 與本 spec 的落差、補未列任務

## 驗收對照

| Story | Tasks | 狀態 |
|-------|-------|:----:|
| US1 捏造內容移除 | T002–T004, T006, T012–T013, T015–T016 | 程式✅ / 測試⬜ |
| US2 fail-closed | T005, T014 | 程式✅ / 測試⬜ |
| US3 禁語與時效 | T007–T008, T017 | 程式✅ / 測試⬜ |
