# Tasks: 小型股候選池

**Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Phase 1 — 前置

- [x] T001 以定案的流動性帶（1,000 萬–5,000 萬）重跑 sleeve 回測，確認扣成本後淨報酬為正（SC-004）
- [x] T002 `backend/config.py`：`enable_smallcap_candidates` / `smallcap_min_amount_twd` / `smallcap_top_k`；`strategy_calibration.SMALLCAP_MIN_TWD` 改讀設定並重訓帶模型（FR-009、FR-011）

## Phase 2 — 產生名單（US1）

- [x] T003 [US1] `backend/processor/tw_features.py`：`_smallcap_pool()`，定義與回測候選一致；`features.tw.smallcap`（FR-001）
- [x] T004 [US1] `backend/ai/prompts.py`：`_budget_features` 拿掉 `tw.smallcap`（FR-010：決策層不得增刪重排）
- [x] T005 [US1] `backend/data_sources/trading_restrictions.py`：處置股與變更交易名單，任一來源失敗回 None（FR-007）
- [x] T006 [US1] `backend/reports/smallcap_sleeve.py`：`build()` 四道關卡 + 確定性訊號文字（FR-001–FR-003）
- [x] T007 [US1] `backend/reports/morning_brief.py`、`markdown_builder.py`、`templates/report.html`、`discord_summary.py`：落地與三個介面的呈現，含低流動性提示與「今日不列」的原因（FR-003）

## Phase 3 — 獨立追蹤與停損（US2）

- [x] T008 [US2] `backend/reports/backtest.py`：`scorecard["smallcap_items"]`（不併入 `items`）（FR-005）
- [x] T009 [US2] `smallcap_sleeve.live_stats()` / `live_gate()`；`evaluate_effectiveness` 寫入 `evaluation.json["smallcap"]`（FR-005、FR-006）

## Phase 4 — 驗證

- [x] T010 [P] `tests/test_smallcap_sleeve.py`：候選池定義、不進 prompt、排序取前 K、受限排除、四道關卡各自的不列原因、空池不遞補、成效統計與停損、scorecard 隔離、Markdown 呈現、名單 fail-closed
- [x] T011 真實資料乾跑（不經 LLM）：候選池、排除、名單（結果見 plan.md）
- [x] T012 rebuild 部署後，以隔日排程晨報確認 `tw_smallcap` 落地與三個介面呈現（SC-001、SC-002）——10/14、10/15 晨報 JSON 與 Markdown 皆有 `tw_smallcap`
- [ ] T013 上線滿 20 個到期交易日後檢視 `evaluation.json["smallcap"]`（SC-003）

## 第二階段（未開始）

- [ ] T014 決策層為入選個股撰寫說明與風險（不得增刪重排）；輸出經 Symbol Guard 與敘事數字驗證（FR-004、FR-010 後半）；單篇成本增加須 ≤ NT$3（SC-005）
- [x] T015 上櫃變更交易名單來源——TPEx `tpex_cmode`（變更交易／分盤／管理股票／停止交易任一旗標即排除）；2026-10-19 實測名單擴至 52 檔，10/14–10/19 已列名單皆未踩到
