# Feature Specification: 決策層新增 OpenAI 供應商（方案 B）

**Feature Branch**: `026-openai-decision-layer`

**Created**: 2026-10-20

**Status**: Draft

**Input**: User description: "決策+查證層新增 OpenAI 供應商選項（方案B）。保留 Gemini + google_search 召回層與 Gemini 降級路徑不變；新增 OpenAI 決策實作（預設 gpt-6.1-sol），供應商可切 anthropic/openai/gemini，預設仍為 anthropic 以便 A/B。OpenAI 無可指定 URL 的 web_fetch，查證改用本地抓頁工具（白名單＝facts pack 引用的 URL）。成本計價新增 OpenAI。/ask 也可走 OpenAI。目標：同一份資料對照 Opus 5 vs gpt-6.1-sol 5–10 天，比成本、裁決率、guardrail 違規數，再決定是否切換預設。使用者尚無 OpenAI API key，全程需可用 mock。"

## 背景與問題

晨報決策層目前是 Claude Opus 5，10 月實測單篇 **約 NT$26**（決策 ≈ 24.5、Gemini 召回 ≈ 1），
月投影 NT$600–650，貼近憲章 II 的月上限 NT$800，且只夠每天跑一次。

2026-10-19 評估（`~/.claude/plans/gemini-claude-openai-gpt-typed-pizza.md`）以官方牌價估算：
OpenAI `gpt-6.1-sol`（$2 / $10 per MTok，cached $0.10）同樣 token 量下單篇約 **NT$12（-55%）**。
但這只是牌價推算——成本主體是輸出／推理 token，各模型差異大，**品質與實際成本都必須實測**。

刻意選「方案 B」而非全換：

- **保留 Gemini + Google 搜尋召回**：台灣中文財經新聞覆蓋已知可用；OpenAI 搜尋對台股媒體的覆蓋未知。
  且 Gemini 召回每篇僅約 NT$1，換掉幾乎省不到錢。
- **保留跨供應商備援**：決策層故障仍可退回 Gemini 出報（憲章 I 的降級要求不變）。
- **召回與決策仍是不同供應商**：交叉查證的前提不受影響。

已知的結構性差異（決定本 spec 範圍）：OpenAI 沒有「只能開啟對話中已出現 URL」的伺服器端抓頁工具。
現行查證閉環（spec 022/023）依賴這個限制來保證「模型只能核對召回層真的引用過的來源」，
所以本 spec 必須在系統端重建同等（或更嚴格）的稽核閉環，不能退化成「模型自由上網」。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 用 OpenAI 產出一份完整晨報 (Priority: P1)

Jay 把決策層供應商設成 OpenAI 後，每日晨報照常產出：同樣的結構化內容、同樣的查證欄位、
同樣的 guardrail 檢查，報告上清楚標示這篇由哪個供應商與模型產生、花了多少錢。

**Why this priority**: 沒有這條就沒有任何可比較的東西；它也是之後切換預設的最小可行版本。

**Independent Test**: 在無真實 API key 的環境下，以模擬的 OpenAI 回應跑完整晨報管線，
產出的報告通過既有 schema 驗證與 guardrail，且 `decision_provider` 標為 openai、成本記入 openai 桶。

**Acceptance Scenarios**:

1. **Given** 供應商設為 openai、facts pack 有 5 則線索，**When** 晨報執行，**Then** 產出符合既有報告結構的晨報，
   每則被採用的外部事件都有 `fact_checks` 紀錄，報告標示 provider=openai 與實際模型名。
2. **Given** 供應商設為 openai 但 OpenAI 回錯誤或逾時（重試用盡），**When** 晨報執行，**Then** 退回 Gemini 降級路徑產出晨報，
   報告標示為降級，且已發生的查證行為仍被記錄。
3. **Given** 供應商維持預設，**When** 晨報執行，**Then** 行為與現行完全相同（仍為 Claude），未設定 OpenAI key 不影響啟動。
4. **Given** 預算降級（節儉模式）被觸發，**When** 以 openai 執行，**Then** 關閉查證與補漏搜尋、降低推理強度，與現行節儉模式語意一致且對使用者可見。

---

### User Story 2 - 查證閉環在 OpenAI 下仍可稽核 (Priority: P1)

決策模型只能開啟召回層 facts pack 中引用過的網址來核對；每一次開啟（成功、失敗、被拒）
都由系統端記錄成工具行為，而非採信模型自述。

**Why this priority**: 查證閉環是分層設計存在的理由（憲章 I）。換供應商若讓模型能自由開任意網址，
或讓查證次數變得不可量測，等於換掉了這個產品的核心保證，成本再低也不可接受。

**Independent Test**: 以模擬回應讓模型要求開啟 (a) facts pack 內的網址、(b) 不在清單內的網址、
(c) 內網／本機位址，驗證 (a) 被執行並記錄、(b)(c) 被拒並記錄為拒絕，且次數上限生效。

**Acceptance Scenarios**:

1. **Given** facts pack 引用了 URL X，**When** 模型要求開啟 X，**Then** 系統抓取原文、轉為純文字並截斷到上限後回給模型，記錄一筆成功的查證行為（含網址、時間、內容長度）。
2. **Given** 模型要求開啟不在 facts pack 中的網址，**When** 系統收到請求，**Then** 拒絕抓取、回覆模型「不允許」並記錄一筆被拒的查證行為。
3. **Given** 網址解析到私有／本機／保留位址，**When** 模型要求開啟，**Then** 一律拒絕並記錄。
4. **Given** 每篇查證次數上限為 N，**When** 模型第 N+1 次要求開啟，**Then** 拒絕並記錄為額度用盡（與 spec 023 的「額度不足」分類一致）。
5. **Given** 抓頁逾時、HTTP 錯誤或非文字內容，**When** 查證執行，**Then** 記錄對應的失敗原因，報告層既有的 verification 統計可正確分類。

---

### User Story 3 - 成本可歸因且受預算約束 (Priority: P1)

OpenAI 的花費以正確牌價計入，與 Gemini、Anthropic 分桶顯示；月／日上限與晨報降級邏輯對 OpenAI 同樣生效。

**Why this priority**: 評估「省 55%」是否成立完全仰賴這個數字；計價錯（例如快取 token 重複扣或少算）會讓 A/B 結論與預算閘門同時失效。

**Independent Test**: 以固定的模擬 usage（含快取命中、快取寫入、推理 token、搜尋次數）計算成本，與手算值一致；首頁／報告的分供應商月桶出現 openai。

**Acceptance Scenarios**:

1. **Given** 一次回應的 usage 為 input 60k（其中 cached 40k）、output 18k（其中推理 10k）、搜尋 2 次，**When** 計價，**Then** 成本 = 未命中 input×牌價 + cached×快取價 + output×牌價 + 2×每次搜尋價，cached 不被重複計入。
2. **Given** 未列在價目表的 OpenAI 模型名，**When** 計價，**Then** 以已知最貴檔估算並記錄警告（寧可高估，與現行 Anthropic 處理一致）。
3. **Given** 月餘額低於降級門檻，**When** 以 openai 跑晨報，**Then** 走節儉模式，與現行規則相同。

---

### User Story 4 - 對照實驗與切換決策 (Priority: P2)

Jay 能在 5–10 個交易日內累積兩個供應商的可比資料，並用一個指令看到並排比較：單篇成本、查證裁決率、
guardrail 違規數、輸出 token 量，作為是否切換預設的依據。

**Why this priority**: 這是本 spec 的最終目的，但依賴 US1–US3 先完成；且需要真實 API key 才能產生真資料。

**Independent Test**: 用既有報告（Claude）加上模擬產生的 openai 報告，執行比較指令，輸出兩欄並排的指標表。

**Acceptance Scenarios**:

1. **Given** 觀察窗內有兩個供應商的報告，**When** 執行比較，**Then** 依 provider 分組列出：篇數、平均／中位單篇成本、裁決率（confirmed+contradicted / 線索數）、guardrail 違規數、平均輸出 token。
2. **Given** 影子對照模式開啟（同日同一份 features + facts pack），**When** 晨報執行，**Then** Claude 版照常發布；OpenAI 版以相同輸入另跑一次，結果只存檔（標示 shadow、不發布到 Discord／網頁），成本照實記入 openai 桶。
3. **Given** 影子模式下 OpenAI 那次失敗，**When** 晨報執行，**Then** 不影響 Claude 版發布，失敗原因記錄在影子報告中。
4. **Given** 試驗期結束日已過，**When** 晨報執行，**Then** 影子模式自動停止，月上限回到原值。

---

### User Story 5 - /ask 問答可走 OpenAI (Priority: P3)

Discord 問答的作答模型可設為 OpenAI，意圖分類仍走 Gemini。

**Why this priority**: 問答月花費約 NT$50，省下的絕對金額小；但同一個切換旋鈕若只管晨報會讓設定語意混亂。

**Independent Test**: 以模擬回應呼叫 /ask，回答正常、成本記入 openai 桶、預算閘門照常擋。

**Acceptance Scenarios**:

1. **Given** 供應商為 openai，**When** 使用者提問財經問題，**Then** 以 OpenAI 作答，可用補漏搜尋（有上限），成本記入 openai。
2. **Given** 日上限已滿，**When** 提問，**Then** 照現行規則拒絕。

### Edge Cases

- 未設定 OpenAI key 卻把供應商設為 openai：啟動時不得崩潰；晨報直接走 Gemini 降級並在報告與日誌明確標示原因（而非靜默）。
- 設定了未知的供應商名稱：維持現行 fail-fast 行為。
- OpenAI 回覆不符合結構（JSON 不合法或缺欄位）：視為決策層失敗，走降級路徑；不得寫出半份晨報。
- OpenAI 婉拒（安全拒答）：與 Claude refusal 同等處理，歸類為中立例外。
- 工具迴圈失控（模型不停要求工具）：有總輪數上限，超過即終止並走降級；已發生的查證行為仍記錄。
- 抓回的頁面非常大或是 PDF／圖片：截斷或記錄為非文字，不得把整頁塞回模型。
- 同一 URL 被要求開啟多次：重複開啟仍算一次額度（避免模型以重開燒額度），或直接回快取內容；行為需一致且被記錄。
- facts pack 為空（召回層全失敗）：查證白名單為空，模型無法開任何網址；晨報仍可只憑 features 產出（同現行）。
- 推理 token 超過輸出上限導致回應截斷：視為失敗，不得當成功晨報發布。
- OpenAI 牌價變動或優惠到期：價目表需有核對日期註記；與 Gemini 別名換代偵測同等，回應中的實際模型版本與價目表不符時記警告。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 系統 MUST 支援決策層供應商 `openai`，與既有 `anthropic`、`gemini` 並列，由單一設定值切換；**預設值維持 `anthropic`**。
- **FR-002**: OpenAI 決策實作 MUST 產出與現行相同結構的晨報草稿（同一份 schema、同一套 guardrail、同一組報告欄位），下游報告產生、Discord 摘要、網頁呈現不需因供應商而分支。
- **FR-003**: 決策模型名稱、推理強度（晨報與問答分開）、補漏搜尋次數上限、查證次數上限、單頁內容上限 MUST 可由設定調整；預設模型為 `gpt-6.1-sol`。
- **FR-004**: 召回層（Gemini + Google 搜尋）與 Gemini 降級路徑 MUST 維持不變；OpenAI 決策層失敗時 MUST 退回 Gemini 降級路徑，與現行 Claude 失敗時的行為一致。
- **FR-005**: 查證 MUST 只能開啟當篇 facts pack 中引用的網址（白名單）；白名單外、私有／本機／保留位址的請求 MUST 被拒絕。
- **FR-006**: 每一次查證請求（成功、失敗、被拒、額度用盡）MUST 由系統端記錄為查證行為，欄位與 spec 023 的查證紀錄相容，使既有的 verification 統計與報表可直接沿用。
- **FR-007**: 查證抓回的內容 MUST 轉為純文字並截斷到設定上限後才交給模型；非文字內容 MUST 記錄為非文字而非原樣傳入。
- **FR-008**: 補漏搜尋 MUST 有每篇次數上限，次數 MUST 記入遙測與成本。
- **FR-009**: 工具迴圈 MUST 有總輪數上限；超過上限 MUST 終止並視為決策層失敗。
- **FR-010**: 成本追蹤 MUST 新增 OpenAI 價目（input、cached input、cache write、output、每次搜尋），正確處理「input 計數已包含 cached」的語意，推理 token 以 output 計價；成本 MUST 記入 `openai` 供應商桶。
- **FR-011**: 未知 OpenAI 模型名 MUST 以已知最貴檔估價並警告。
- **FR-012**: 月／日上限、`/ask` 預算閘門、晨報降級（節儉模式）MUST 對 OpenAI 同樣生效；節儉模式 MUST 關閉查證與補漏搜尋並降低推理強度。
- **FR-013**: 報告 MUST 記錄決策供應商、請求的模型名與回應中的實際模型版本、以及 token 明細（未命中 input、cached、cache write、output、推理、搜尋次數、查證次數、輪數）。
- **FR-014**: OpenAI 的錯誤 MUST 映射為既有的供應商中立例外（配額、請求被拒、暫時不可用、婉拒），上層降級邏輯不得認得 OpenAI 型別。
- **FR-015**: `/ask` 問答 MUST 可依同一供應商設定走 OpenAI 作答；意圖分類維持 Gemini。
- **FR-016**: 系統 MUST 提供比較指令，依供應商分組輸出：篇數、單篇成本（平均／中位）、裁決率、guardrail 違規數、平均輸出 token。
- **FR-017**: 對照實驗採**同日影子模式**：同一份 features 與 facts pack 由主供應商（Claude）與影子供應商（OpenAI）各跑一次，只發布主供應商版本；影子結果存檔供比較、不得出現在任何使用者可見的發布管道。影子模式 MUST 有明確的試驗結束日，到期自動停止。
- **FR-017a**: 影子模式使日花費約 NT$39、月約 NT$820，超過現行月上限 NT$800。試驗期 MUST 依憲章程序把月上限**暫時**調高（約 NT$1,000），憲章與四處設定同步並註明結束日；試驗結束 MUST 恢復 NT$800。影子那次的花費 MUST 計入月／日總額，且月餘額不足時 MUST 先停影子、不得讓主晨報因此進入節儉模式。
- **FR-017b**: 影子那次 MUST 共用主晨報已取得的 facts pack（不重跑召回層），以確保輸入相同並避免重複召回成本。
- **FR-018**: 所有新增行為 MUST 能在無 OpenAI API key 的環境下以模擬回應完整測試；未設定 key 時系統 MUST 正常啟動。
- **FR-019**: 憲章 Principle I MUST 以 MINOR 版本修訂，明列決策層可選供應商包含 OpenAI，並同步 README／ARCHITECTURE 中的供應商描述。

### Key Entities

- **決策供應商設定**：目前使用哪一家決策模型；值為 anthropic / openai / gemini，預設 anthropic。
- **查證行為紀錄（FetchAttempt）**：每次開啟來源的工具行為——網址、成功與否、失敗原因（含新增的「白名單拒絕」「私有位址拒絕」）、時間、內容長度。沿用 spec 023 結構。
- **查證白名單**：當篇 facts pack 引用的網址集合，只在單篇晨報生命週期內有效。
- **供應商成本桶**：每月依 gemini / anthropic / openai 分開累計的花費。
- **對照比較結果**：觀察窗內依供應商分組的成本、裁決率、違規數、輸出量指標。

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 在無 API key 的環境下，OpenAI 路徑的晨報、查證、計價、降級、問答全部有自動化測試覆蓋；既有測試基線（543 passed）全數維持通過。
- **SC-002**: 預設設定下，晨報與問答的行為、成本、報告內容與導入前無差異（回歸為零）。
- **SC-003**: 白名單外與私有位址的抓頁請求 100% 被拒且 100% 被記錄。
- **SC-004**: 成本計算與手算值誤差為 0（固定 usage 的測試案例），且取得 key 後首週與 OpenAI 帳單對帳差距 ≤ 5%。
- **SC-005**: 取得 key 後完成 5–10 個交易日對照，產出一份並排比較表，能據以回答：OpenAI 單篇成本是否 ≤ Claude 的 60%、裁決率是否不低於 Claude 的 80%、guardrail 違規數是否不高於 Claude。三項都成立才建議切換預設。
- **SC-006**: 試驗期內每月總花費不超過當時憲章 II 生效的（暫時調高後的）上限；主晨報在試驗期內 0 次因影子花費而進入節儉模式。
- **SC-007**: 影子報告 0 次出現在 Discord 或網頁發布內容中。

## Assumptions

- 使用者尚無 OpenAI API key；開發與測試全程以模擬回應進行，真實對照在取得 key 後才開始，不阻擋本 spec 的實作完成。
- 牌價採 2026-10-19 OpenAI 官方定價頁：gpt-6.1-sol input $2 / cached $0.10 / cache write $2.50 / output $10（per MTok）、web search $10 / 1k 次；匯率沿用現行固定 32。
- 抓頁在 backend 容器內進行（從使用者機器的網路出口），不使用付費抓頁服務；JS 動態頁、需登入或擋爬蟲的網站可能抓不到，會被如實記錄為失敗。
- 召回層給的網址已由現行流程解析掉 Google 轉址，白名單直接使用解析後的網址。
- 不刪除 Claude 實作；是否移除留待對照結論後另案決定。
- 召回層換供應商、/ask 意圖分類換供應商不在本 spec 範圍。
- 推理強度預設：晨報 medium、問答 medium，與現行 Claude 設定對齊以利比較。
