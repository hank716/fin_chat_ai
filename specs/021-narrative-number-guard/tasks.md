# Tasks: 晨報敘事數字驗證

**Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Phase 1 — 驗證器（US1）

- [x] T001 [US1] `backend/guardrails/narrative_numbers.py`：`NumberClaim`、規則表、`extract_claims()`、`check_item()`（FR-001–FR-004、FR-006）
- [x] T002 [US1] `backend/guardrails/verify.py`：`run_guardrails()` 接上驗證器，不符記 warning、不改寫（FR-005）；例外記 error（FR-008）
- [x] T003 [P] [US1] `tests/test_narrative_numbers.py`：10 句捏造 ≥8 被標（SC-001）、10 句正確誤報 ≤1（SC-002）、不改寫原文、驗證器例外可見

## Phase 2 — 可彙總（US2）

- [x] T004 [US2] `backend/guardrails/verify.py`：guardrail report 新增 `narrative_number`（checked / mismatched / mismatches）與 counts 兩欄（FR-007）
- [x] T005 [US2] 對 90 篇已落地報告回放，逐筆檢視不符並修正規則；結果記於 plan.md（SC-003）

## Phase 3 — 部署驗收

- [x] T006 rebuild backend image 後，以隔日排程晨報確認 `guardrail.narrative_number` 落地（SC-004）；不為驗收多跑付費晨報——10/14 checked 61/mismatched 0；10/15 checked 35/mismatched 1（6716 量增倍數說明寫 2.77、實際 1.77）
- [x] T007 全套 `pytest -q` 綠燈

## 後續（不在本期）

- 段落 narrative 的跨標的數字（需先解決指代）
- 累積一段時間後若誤報維持在零，另案評估把「只警示」升級為移除該句
