# Quickstart: spec 026 驗證步驟

## A. 無 API key（實作完成即可跑）

1. 重建 backend（新增 `openai`、`beautifulsoup4` 依賴）：
   ```
   docker compose build backend && docker compose up -d backend
   ```
2. 全部測試：
   ```
   docker compose exec -T backend pytest -q
   ```
   期望：≥ 543 + 新增測試全數通過；既有測試期望值未被修改（SC-002）。
3. 重點測試檔（新增）：
   - `tests/test_openai_client.py`：schema strict 化、工具迴圈、輪數上限、incomplete、refusal、錯誤映射、usage 加總。
   - `tests/test_fetch_tool.py`：白名單、私有 IP（含轉址跳到 127.0.0.1）、重複 URL、額度、逾時、非文字、截斷。
   - `tests/test_cost_openai.py`：固定 usage 手算比對（含 cached + write 互斥）、未知 gpt-* 以最貴檔估、`provider_of`。
   - `tests/test_shadow_brief.py`：影子在發布後執行、寫入 `shadow/`、不出現在 `list_reports()`、預算不足跳過、影子例外不影響主報告、到期自動停。
   - `tests/test_provider_compare.py`：配對、門檻判定、空目錄非零 exit。
4. 未設 key 的冒煙：`LLM_DECISION_PROVIDER=openai` 且 `OPENAI_API_KEY` 空，backend 能啟動、`/health` 200；觸發晨報應產出 `decision_provider=gemini-fallback` 的報告，markdown 上可見降級提示。驗完改回 `anthropic`。

## B. 取得 API key 之後

1. `.env` 加（不可行內註解）：
   ```
   OPENAI_API_KEY=sk-...
   OPENAI_SHADOW_UNTIL=2026-11-07
   MONTHLY_COST_LIMIT_TWD=1000
   ```
   並同步 `.env.example`、`backend/config.py` 預設註解、憲章 II（試驗期條文）、README。
2. `docker compose up -d backend`，手動觸發一篇晨報。檢查：
   - 主報告 `cost.decision_provider == "anthropic"`，`shadow.status == "ok"`。
   - `storage/reports/shadow/<id>_openai.json` 存在，`verification` 有 confirmed/contradicted，`cost.tokens.cached_tokens > 0`（第 2 輪起命中快取）。
   - 網頁首頁、Discord 都沒有影子內容（SC-007）。
3. 首篇對帳：OpenAI 後台當日金額 vs 影子 `cost.decision_twd`，差距 ≤ 5%（SC-004）。差距大先檢查 research R5 的 `input_tokens` 是否含 cache write。
4. 每日看一次：
   ```
   docker compose exec -T backend python -m reports.verification_stats /data/reports/shadow --since 20261022
   docker compose exec -T backend python -m reports.provider_compare --since 20261022
   ```
5. 試驗期副作用：日上限 45 扣掉主＋影子約 38，`/ask` 當日只剩約 NT$7，被擋屬預期。
6. 5–10 個交易日後依 `provider_compare` 的三條門檻決定：
   - 成立 → 另開 commit 把 `LLM_DECISION_PROVIDER` 預設改 openai、`OPENAI_SHADOW_UNTIL` 清空、月上限改回 800（憲章、四處同步）。
   - 不成立 → 清空 `OPENAI_SHADOW_UNTIL`、上限改回 800，結論寫入 spec 026 與記憶。
