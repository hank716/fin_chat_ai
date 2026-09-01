# AI 多市場研究助理 (fin_chat_ai)

家庭內部使用的 AI 多市場研究助理：每日市場晨報 → Discord 摘要 + Web Report。
資料整理員 + 市場研究助理 + AI 分析師 + 跨市場觀察員，**非**自動交易 / 保證獲利系統。

- **規格（單一真相來源）**：[`specs/`](specs/README.md) — 逐功能 spec/plan/tasks（Spec Kit）
- 開發憲章：[`.specify/memory/constitution.md`](.specify/memory/constitution.md)
- 背景/歷史規格：[`design_docs.md`](design_docs.md)（衝突時以 `specs/` 為準）
- 架構決策 / 平台層：[`ARCHITECTURE.md`](ARCHITECTURE.md)

## 開發流程（Spec-Driven / Spec Kit）

新功能一律走 Spec Kit 流程（指令為 Claude Code skills）：

```text
/speckit-specify → /speckit-clarify → /speckit-plan → /speckit-tasks → /speckit-analyze → /speckit-implement
```

既有 M0–M9 功能採「回溯補規格」建立基線（狀態見 [`specs/README.md`](specs/README.md)）。
commit 遵循 Conventional Commits，並引用對應 `specs/NNN`。

## 技術重點

- **儲存**：local parquet = SSOT（無 Postgres）/ Supabase 暖層 index / pCloud 冷備份
- **LLM**：系統內只用 Gemini；Claude 走 Web 報告底部「可複製深度分析 prompt」由使用者手動觸發
- **資料層**：移植姊妹專案 finflow_ai 的抓取/正規化/rate-limit 層（複製改寫，非 import）

## 建構進度（垂直切片，見 ARCHITECTURE.md §5）

- [x] **M0** — repo 骨架 + docker-compose（backend + redis）+ FastAPI `/health`
- [x] M1 — 黃金路徑垂直切片 ✅ (yfinance → intermarket features → Gemini 結構化 → md/json/copy-for-AI → Web SSR；`POST /brief/morning`、`GET /report/{id}`、`/brief/latest`)
- [x] M2 — 移植 finflow 抓取層 ✅ (rate_limiter/twse/finmind + parquet sink + storage_monitor + `/storage`) + US/crypto loader + universe 過濾 + FinMind 歷史 backfill
- [x] **M2-report** — 完整台股晨報 ✅ (台股 universe/族群 + 價格/籌碼/融資券 backfill + tw_features + 新聞 fact 層 + 敘事報告 BriefResult：正向/負向候選、資券比、跨市場連動)
- [x] M3 — Scheduler + catch-up ✅ (APScheduler 每日 08:30 + 啟動補產；`GET /brief/status`)
- [x] M4 — Discord 互動 bot + 成本上限 ✅ (每日摘要推送 §6.3 + `/ask` grounded Q&A + per-user 成本上限 + 頻道/使用者權限；FinBot 已上線、每日摘要實測推送成功)
- [x] M5 — Guardrail + 新聞分層 ✅ (六道 verification guard：source/metric/symbol/news-citation/advice/causality；新聞 authoritative/social 分層；報告顯示攔截狀態)
- [x] M6 — Supabase publish + pCloud backup + Cloudflare Access ✅ (pCloud 報告備份 + Cloudflare tunnel/Access + Supabase report_index 發布 + 首頁歷史列表；report_index 實測寫入成功)
- [x] M8 — 全台股 universe + 基本面 + 多 crypto ✅ (FinMind 全清單2728檔/57產業別族群；TWSE/TPEx 市場級回補；tw_features 全市場 movers/sectors(聚焦曝光給AI)；月營收 YoY/MoM on-demand；ETH/SOL)
- [x] M7 — 檔案級保留/清理 + pCloud 回補 ✅ (retention：本機留最近90篇報告+清 adhoc parquet 快取；pCloud 冷儲存回補；晨報只在台股交易日產生；研究工具 google_search/url_context/code_execution)
- [x] **M9 — 完整財報（季報 / 損益表 / 資產負債表 / 現金流 / 股利）** ✅（on-demand，焦點標的）

## M9 — 完整財報 ✅

M8 只做了**月營收 YoY/MoM**；M9 補上**季財報**，對焦點標的（movers / watchlist / 問答標的）
on-demand 抓取並算衍生指標，餵進 grounded 研究稿，仍受 guardrail metric/source 驗證。

**資料來源（FinMind dataset）**：

| 資料 | FinMind dataset | 產出欄位（`fundamentals.*`） |
|------|-----------------|------|
| 損益表（季） | `TaiwanStockFinancialStatements` | `eps_quarter`、`eps_ttm`（近四季）、`gross_margin_pct` / `operating_margin_pct` / `net_margin_pct`、`fiscal_quarter` |
| 資產負債表 | `TaiwanStockBalanceSheet` | `debt_ratio_pct`（總負債/總資產） |
| 現金流量表 | `TaiwanStockCashFlowsStatement` | `op_cashflow_ttm_100m`、`free_cashflow_ttm_100m`（營業現金流 − 資本支出） |
| 股利政策 | `TaiwanStockDividend` | `dividend.{year, cash_per_share, stock_per_share}` |

**實作**：

1. `finmind_loader` 新增 `get_financial_statements / get_balance_sheet / get_cash_flows / get_dividend`（單檔、走 rate_limiter，FinMind 免費 600 req/hr）。
2. `processor/fundamentals.py` 拆 `_build_revenue` + 新增 `build_financials()`（`lru_cache`，季報季更當日不重抓）；長格式 `type/value` 寬鬆比對（精確→子字串 fallback），任一資料源失敗只略過該區塊。
3. `morning_brief`（經 `tw_features`）與 `ask` 自動帶入；prompt「基本面觀察」段更新，明列三率/負債比/EPS_TTM/自由現金流為**衍生指標**、據實引用勿外推。
4. **guardrail**：財報數字皆掛在 `features.tw.stocks[].fundamentals.*` 路徑，AI 引用時走既有 metric/source path 驗證（不存在的欄位會被攔除）。
5. 全市場 2000+ 檔不全抓 → 維持 on-demand + 焦點標的策略（對齊 design §5.2）。

**驗收**：焦點標的可顯示近四季 EPS / 三率 / 負債比 / 自由現金流 / 股利；衍生指標離線單元驗證數值正確。

## 成本 / 預算現況

全站總花費（晨報 + 所有人問答）以 token × 模型費率估算（`backend/cost/tracker.py`），上限在 `.env` 可調（`backend/config.py`）：

- **每日上限** `DAILY_COST_LIMIT_TWD = 45`（**只約束 `/ask`**，見下）
- **每月上限** `MONTHLY_COST_LIMIT_TWD = 800`（憲章 II 的硬性約束，`.env` 須與之一致）
- **降級緩衝** `BRIEF_DEGRADE_RESERVE_TWD = 45`（月餘額低於此值 → 晨報跑節儉模式）

> **實測單價（2026-08-12 盤點）**：晨報＝Gemini flash 廣度召回（NT$0.2–5）+ Claude Opus 5
> 單次決策與查證（NT$23–37）。近四篇單日總額 **NT$26–39、均值 32.5**，月約 20.5 個交易日
> → 投影約 **NT$666/月**。上限 800 是照這個跑率訂的，只夠**每天跑一次**晨報。
>
> 成本主體在 **output/thinking**（以 output 費率計價），不在 input——晨報開了 prompt cache，
> 快取讀取 0.1× 打在查證工具迴圈的前綴重讀上，實測單篇省下約 NT$61。**不要關掉它**。
>
> 預算優先序為 **晨報 > 問答**：晨報不受 `check_budget()` 攔截（那只擋 `/ask`），改由
> `brief_should_degrade()` 在月餘額見底或當日已花滿日上限（補跑）時降級——照常出報，
> 但關掉外部事件與連網查證。降級會直接標在報告、Discord 與網頁上，不是無聲發生。
> 每份晨報 JSON 的 `cost.month_remaining_twd` 也會攤出剩餘額度。

## 本機啟動（M0）

```bash
cp .env.example .env          # 填入實際憑證（.env 已被 .gitignore 排除）
docker compose up -d --build
curl -s localhost:8000/health  # 預期 200 {"status":"ok","redis":"up",...}
```

不用 Docker：

```bash
cd backend
pip install -r requirements.txt
REDIS_URL=redis://localhost:6379/0 uvicorn main:app --reload
```

## 本機測試

測試**一律跑在 `.venv` 裡**，且該 venv 必須釘 **Python 3.12**——與正式 image 同版本。原因很實際：
`backend/requirements.txt` 釘 `pandas>=2.2,<3.0`，而 pandas 2.x 沒有 cp314 wheel，用系統的新版
Python 只會裝出 pandas 3.x，等於拿一個跟線上差一個 major 版本的環境在驗收。

```bash
python -m pip install --user uv          # 只當工具用，免管理員權限
uv python install 3.12
uv venv .venv --python 3.12
uv pip install --python .venv -r backend/requirements.txt -r requirements-dev.txt
.venv/Scripts/python -m pytest -q        # Linux/macOS: .venv/bin/python
```

`requirements-dev.txt` 只放測試工具（pytest）；執行期依賴一律以 `backend/requirements.txt` 為準，
兩份分開才不會讓測試環境悄悄多出正式環境沒有的套件。

驗收標準是 `pytest -q` **全綠且零 collection error**——不要用 `--ignore` 跳過跑不起來的檔案，
那會把「環境沒裝好」偽裝成「測試通過」（2026-08-30 就是這樣讓 10 個檔長期沒被執行）。
確認版本對齊：

```bash
.venv/Scripts/python -c "import sys,pandas,pyarrow;print(sys.version.split()[0],pandas.__version__,pyarrow.__version__)"
docker exec ai-market-backend python -c "import sys,pandas,pyarrow;print(sys.version.split()[0],pandas.__version__,pyarrow.__version__)"
```

## 目錄結構

```
backend/    FastAPI：api / router / data_sources / processor / ai / guardrails / reports / storage / cost
bot/        Discord 互動 bot（M4）
scheduler/  排程 + catch-up（M3）
configs/    universe / features YAML
storage/    本機 parquet / cache / reports（gitignore，runtime 產生）
```
