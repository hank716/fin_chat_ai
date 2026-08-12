# Tasks: 查證閉環有效性

**Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

**格式**：`- [ ] T### [P] [US#] 說明（檔案路徑）`。`[P]` = 可與同批其他 `[P]` 併行（不同檔案、無未完成依賴）。

---

## Phase 1 — Setup

無新依賴、無新服務、無 migration。`anthropic` SDK 已在用，本案只是多讀既有回應欄位。

- [ ] T001 確認容器內 SDK 型別可用：`docker compose exec -T backend python -c "from anthropic.types import WebFetchToolResultBlock, WebFetchBlock, WebFetchToolResultErrorBlock; print('ok')"`。若 import 失敗代表 SDK 版本與 plan 假設不符，**停下來重新評估 plan 的架構決策**，不要改用讀模型 note 的替代方案（違反 FR-006）。

---

## Phase 2 — Foundational（阻塞所有 user story）

把「工具實際發生了什麼」從 content blocks 抽出來。US1 與 US2 都建立在這層資料上。

- [ ] T002 `backend/ai/claude_client.py`：新增 `FetchAttempt` 輕量結構（`url` / `ok` / `error_code` / `retrieved_at`），以 `tool_use_id` 配對 `server_tool_use`(name=`web_fetch`) 與 `web_fetch_tool_result`。失敗時 URL 取自 `server_tool_use.input["url"]`（錯誤 block 沒有 `url` 欄位）。
- [ ] T003 `backend/ai/claude_client.py`：`_run()` 跨 `pause_turn` 續跑輪次**累加** attempts（沿用既有 `fetches` 計數器的累加點），並在每條離開 `_run` 的路徑回傳（含 refusal、續跑用盡兩條例外路徑）——與 `_stamp_tool_counts` 同樣的完整性要求。
- [ ] T004 `backend/ai/claude_client.py` + `backend/ai/llm_client.py`：新增獨立回傳通道把 attempts 帶到呼叫端。**不得**塞進 `usage`（`dict[str, int]`，且在 `morning_brief` 被逐鍵相加，塞 list 會炸）。同步更新 `generate_structured` / `generate_answer` / `AnthropicDecisionLLM.draft_brief` 的型別註記。
- [ ] T005 `backend/ai/llm_client.py`：`GeminiDecisionLLM.draft_brief` 回傳空 attempts（降級路徑本就不做查證），讓兩個實作型別一致，呼叫端不必分支。
- [ ] T006 [P] `tests/test_claude_client.py`：新增 attempts 配對測試——成功 block 取到 url、錯誤 block 取到 error_code、跨 `pause_turn` 累加、refusal 路徑也帶出 attempts。
- [ ] T007 [P] `tests/test_provider_switch.py`：釘住 Gemini 降級路徑回空 attempts 且型別與 Anthropic 路徑一致。

---

## Phase 3 — US1 可觀測性（P1）

**Story Goal**：能分辨「已核對」與「未查證」，並能跨報告統計失敗原因分布。

**Independent Test**：跑一篇晨報，檢查每則未成功裁決的線索都帶可機讀的失敗原因；連續數日後可產出分布統計。不需調整任何參數。

- [ ] T008 [US1] `backend/reports/verification_stats.py`（新檔）：實作結局分類函式，把 attempts 以 URL 對回 `fact_checks`，輸出 plan 表定的七類之一（`confirmed` / `contradicted` / `checked_insufficient` / `unchecked_budget` / `unchecked_unreachable` / `unchecked_transient` / `unchecked_other`）。FR-001、FR-002。
- [ ] T009 [US1] `backend/reports/verification_stats.py`：實作「無 attempt」的兩種分流——該篇額度已耗盡 → `unchecked_budget`；額度未耗盡 → `unchecked_other`（模型主動放棄查證）。這兩者混淆會讓 US3 得出錯誤結論。
- [ ] T010 [US1] `backend/reports/morning_brief.py`：`_verification_stats()` 改用 T008 的分類，`cost.verification` 擴充為每則線索的結局 + 來源識別 + 該篇額度上限與實際用量（FR-003、FR-004）。保留既有欄位以免既有測試與渲染壞掉。
- [ ] T011 [US1] `backend/reports/morning_brief.py`：告警改用分類——`unchecked_budget` 出現時明確 warn「額度不足」，`unchecked_unreachable` 出現時 warn 並列出來源，取代現行只看 `fetch_requests == 0` 的粗略判斷。
- [ ] T012 [US1] `backend/reports/verification_stats.py`：實作跨報告彙總（讀 `storage/reports/*.json`），輸出各結局分類次數、各來源成功/失敗次數與原因分布（FR-005、SC-002）。**必須排除**轉址 bug 期（8/07 及更早）與降級供應商產出的報告，否則污染基準。
- [ ] T013 [US1] `backend/reports/verification_stats.py`：加 `python -m reports.verification_stats` CLI 入口，印出人可讀的彙總表。
- [ ] T014 [P] [US1] `tests/test_verification_stats.py`（新檔）：七類分類的判定測試，含「無 attempt + 額度耗盡」與「無 attempt + 額度未耗盡」的分流。
- [ ] T015 [P] [US1] `tests/test_verification_stats.py`：彙總測試——舊報告缺欄位要容忍（沿用 `test_missing_cost_block_is_tolerated` 的寬容度）、線索數 0 時分母不得除零（Edge Case）、同一來源支撐多則裁決時不重複計次（Edge Case）。

---

## Phase 4 — US2 誠實性（P1）

**Story Goal**：未查證的內容不被當成已查證閱讀。

**Independent Test**：構造一篇「所有線索都未成功查證」的報告，確認三個介面都明確標示。

- [ ] T016 [US2] `backend/reports/degradation.py`：擴充 `degradation_notes()`，依 T008 的分類產生提示——區分「額度不足未查證 N 則」與「來源無法開啟 N 則」，取代現行單一的「未實際開啟任何來源」（FR-007）。
- [ ] T017 [US2] `backend/reports/degradation.py`：全數未查證時，產生等同「本篇無經查證外部事件」的提示（FR-008）。
- [ ] T018 [US2] `backend/guardrails/verify.py`：`run_guardrails()` 新增查證結局參數，未經核對的線索若被引用為選股理由或方向判斷依據，比照現行 evidence guard 擋下或降級標示（FR-009，憲章 III fail-closed）。注意現行簽章是 `run_guardrails(result, features)`，呼叫點在 `morning_brief.py:481`。
- [ ] T019 [US2] `backend/reports/morning_brief.py`：把查證結局傳入 `run_guardrails()`，並確認 guardrail 的 `error_count` / `warning_count` 統計涵蓋新守則。
- [ ] T020 [P] [US2] `tests/test_degradation_notes.py`：新增分類提示案例——只有額度不足、只有來源開不了、兩者混合、全數未查證。
- [ ] T021 [P] [US2] `tests/test_guardrails.py`：釘住「未經查證線索被引用為選股理由 → 擋下」，以及「已核對線索正常放行」不被誤擋。

---

## Phase 5 — 觀察窗（硬性等待，非工作項）

- [ ] T022 部署 Phase 2–4 至正式環境（`docker compose up -d --build`），確認當日晨報的 `cost.verification` 帶有新分類欄位。
- [ ] T023 累積 **≥10 個交易日**的報告後，執行 T013 的 CLI 產出彙總，記錄於本檔下方「觀察結果」段落。SC-002 要求「額度不足」與「來源無法開啟」兩者相加涵蓋 95% 以上失敗案例。
- [ ] T024 依 T023 的資料**判定 US3 / US4 是否執行**。若資料顯示某假說不成立，對應 Phase **取消而非硬做**，並在本檔記錄取消理由。

> ⚠️ Phase 6、7 在 T024 完成前**不得動工**。無遙測依據的調參違反憲章 II，也正是 8/07 那次
> 把 `max_uses` 從 12 降到 3、把 0% 成功率誤歸因為來源品質的成因。

---

## Phase 6 — US3 額度配置（P2，**條件性**）

**執行條件**：T023 資料顯示 `unchecked_budget` 為失敗主因。

- [ ] T025 [US3] `backend/config.py` + `.env.example`：依 T023 資料調整查證額度配置；若採「隨線索數調整」則新增對應設定項（FR-011）。變更說明中**必須引用** T023 的具體數據（FR-010、憲章 II）。
- [ ] T026 [US3] 以調整前後各 ≥5 個交易日對照裁決成功率與單篇成本；月投影超出上限時不予採用或附成本回收方案（FR-012、SC-005）。
- [ ] T027 [US3] `specs/023-verification-efficacy/tasks.md`：記錄調整前後數據與最終決策，作為下次調參的依據。

---

## Phase 7 — US4 召回層來源可核對性（P3，**條件性**）

**執行條件**：T023 資料顯示特定來源系統性無法開啟（注意現有反證：同一新聞網域在額度 12 時成功、額度 3 時失敗，故此條件需要 T023 在**額度充足**的前提下仍觀察到來源失敗才成立）。

- [ ] T028 [US4] `backend/ai/retrieval.py`：於召回層提示中加入來源偏好，降低系統性無法核對來源的優先序（FR-013）。
- [ ] T029 [US4] `backend/ai/retrieval.py`：確保重大事件即使只有不可核對來源報導仍會被提出（標示為無法核對），不得靜默丟棄——覆蓋率不得為成功率犧牲（FR-013、SC-007）。
- [ ] T030 [P] [US4] `tests/test_facts_pack.py`：釘住「只有不可核對來源的事件仍會出現在 facts pack」。

---

## Phase 8 — Polish

- [ ] T031 [P] `README.md`：成本/預算段落補充查證成功率的現況與量測方式。
- [ ] T032 [P] `backend/config.py`：更新 `claude_brief_fetch_uses` 上方那段註解——現行內容主張「真正該修的是召回層的 URL 品質，不是多買幾次 fetch」，該主張寫於轉址 bug 期、未經驗證，應改為引用 T023 的實測結論。
- [ ] T033 全套測試綠燈（容器內 `python -m pytest`），且 `cost.verification` 的舊報告相容性經 T015 驗證。

---

## Dependencies

```
Phase 1 (T001)
   └─> Phase 2 (T002→T003→T004→T005, T006/T007 併行)
          ├─> Phase 3 US1 (T008→T009→T010→T011→T012→T013, T014/T015 併行)
          └─> Phase 4 US2 (T016→T017, T018→T019, T020/T021 併行)  ※ 需要 T008 的分類
                 └─> Phase 5 觀察窗 (T022→T023→T024)
                        ├─> Phase 6 US3（條件性）
                        └─> Phase 7 US4（條件性）
                               └─> Phase 8 Polish
```

US1 與 US2 都依賴 T008 的分類函式，但彼此獨立：可先交付 US1（只落地資料不改渲染），
也可先交付 US2（只改渲染不做彙總）。兩者同一次部署最省事。

## 併行機會

| 批次 | 可同時進行 |
|---|---|
| Phase 2 測試 | T006、T007（不同測試檔） |
| Phase 3 測試 | T014、T015（同檔不同函式，可由同一人一次寫完） |
| Phase 4 | T016/T017（degradation）與 T018/T019（guardrail）分屬不同模組，可併行 |
| Phase 4 測試 | T020、T021（不同測試檔） |
| Phase 8 | T031、T032（不同檔案） |

## Implementation Strategy

**MVP = Phase 1 + 2 + 3（US1）**。只做到這裡就已交付價值：失敗原因變成可稽核的資料，
且憲章 II 要求的「調參需遙測依據」得到滿足。US2 強烈建議同批交付（零成本、修的是誠實性），
但技術上可分開。

**不要跳過 Phase 5 直接做 Phase 6。** 本案存在的理由就是上一次無依據調參的後果。

---

## 觀察結果

<!-- T023 完成後填寫：各結局分類次數、各來源成功率、假說 A/B 的判定 -->

_（待 T023）_
