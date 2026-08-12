# Specification Quality Checklist: 查證閉環有效性

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-12
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Validation Notes

**第 1 輪發現並修正的問題**：

1. *實作細節洩漏*：初稿的 FR 直接寫 `web_fetch`、`url_not_allowed`、`claude_brief_fetch_uses`、
   `cost.verification` 等實作識別字。已改寫為「連網查證」「來源無法開啟」「查證額度配置」
   等中性描述；工具與欄位名稱只保留在「背景與問題」的證據表中作為既有事實引用，
   不進入需求條文。
2. *成功標準含技術指標*：初稿 SC 寫「`fetch_requests` 欄位存在」。已改為使用者/維運者
   可觀察的結果（SC-001 的 1 分鐘判定、SC-003 的不展開結構化資料即可得知）。
3. *條件性工作未標示*：US4（召回層來源品質）初稿寫成必做項。已明確標示為條件性，
   並在 Why this priority 說明「現有反證顯示來源品質可能不是主因，貿然實作可能是解錯問題」。

**刻意未使用 [NEEDS CLARIFICATION] 的判斷**：

本 spec 最大的未知是「額度不足 vs 來源打不開，何者為主因」。這個問題**不應**由使用者澄清
——它是實證問題，且 US1 的整個存在理由就是產生回答它所需的資料。把它寫成 clarification
會要求使用者猜測一個應該用資料回答的問題。故改以「US1 為 P1、US3/US4 為條件性」的
結構表達這個不確定性。

**與憲章的對照**：

- 憲章 I（召回與決策分層互為稽核）：本 spec 修的正是這個稽核閉環實際失效的部分。
- 憲章 II（成本紀律；max_uses 調整 MUST 有遙測依據）：FR-010 直接落實此要求；
  FR-012 落實「新增成本必須說明如何回收」。
- 憲章 II（降級 MUST 對使用者可見）：FR-007、FR-008 延伸至「未查證」這個新的降級面向。
- 憲章 III（guardrail fail-closed）：FR-009 要求未查證線索不得被當已查證事實引用。

## Notes

- 所有項目通過，可進入 `/speckit-plan`。
- 建議 plan 階段特別注意：US1 的資料收集需要時間（SC-002 要求 10 個交易日），
  因此 US1 與 US2 應先實作並部署，US3/US4 待資料就緒後再排。
