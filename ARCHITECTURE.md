# 架構設計：AI 多市場研究助理

> 本文件是對 `design_docs.md` v1.1 的架構審查與強化版。
> design_docs.md 定義「要做什麼」與完整規格；本文件聚焦「架構怎麼搭、為什麼這樣搭、用什麼順序搭」。
> 兩份文件衝突時，以本文件的架構決策為準，規格細節仍回查 design_docs.md。
>
> **重大策略**：資料層**直接移植姊妹專案 `~/Documents/finflow_ai` 的成熟實作**，不重寫。
> 那邊的 TWSE/FinMind loader、rate limiter、pCloud backup/restore、storage monitor 都已實戰驗證。

---

## 1. 架構評估摘要

### 1.1 設計做得好的地方（保留）

- **職責切分正確**：`Data Source → Processor → AI → Guardrail → Report Builder → Web/Discord`，骨架不動。
- **核心原則抓對重點**：AI 不得捏造、新聞必須有來源、跨市場只能說「可能影響」、候選標的不是買進建議。
- **介面分工合理**：Discord 放摘要與連結、Web 放長文與歷史、底部給「Copy for Another AI」分析包。

### 1.2 在原設計上做的關鍵調整

| # | 原設計 | 調整 | 理由 |
|---|--------|------|------|
| 1 | 23 步水平建構（§32） | **垂直切片優先**：先打通最薄端到端，再逐層加厚 | 最早解掉整合風險，最快看到能動的成果 |
| 2 | 自建 loader | **移植 finflow_ai 的「抓取/正規化/rate-limit 層」，落地仍用 local parquet（design_docs §9.3）** | 不重造輪子；finflow 已解掉 rate limit、TW 籌碼格式等最難的坑。只搬「抓取正規化」（storage-agnostic），不搬其 Postgres 落地 |
| 3 | Guardrail 用禁用詞字串比對 | **LLM 輸出結構化 JSON，guardrail 驗證每個 claim 的來源**，禁用詞黑名單退為第二道網 | Fact/Calc/Inference 分層才能機器可驗證 |
| 4 | LLM 在系統內路由 Gemini / Claude | **Bot 全程走 Gemini**；Claude 改成 Web 報告底部一段**可複製的深度分析 prompt，由使用者自己決定**丟不丟 | 系統內無需 Claude API，架構更簡單、成本可控；深度分析交給使用者手動觸發 |

### 1.3 最大的未解風險（需持續關注）

- **可靠性**：8:30 晨報跑在家用 Windows PC 上，關機/睡眠/更新都會開天窗。MVP 先用「本機跑＋強化 catch-up」接受此限制。
- **資料源**：TWSE/TPEx/FinMind 有 rate limit 與格式坑 → **已由移植 finflow 的 rate_limiter + 正規化 loader 緩解**。
- **Future leakage（finflow 的教訓）**：財報/籌碼 feature 的時間索引要用「**資料公布日**」不是「報告期間」，否則回測與分析會偷看未來。移植時一併沿用。
- **新聞可信度**：論壇（PTT/Dcard）內容**不可當事實引用**，只能當情緒訊號 → 見 §4.3 新聞分層策略。

---

## 2. 已確認的架構決策

1. **建構策略 = 垂直切片優先**（非水平 23 步）
2. **儲存 = local parquet（嚴守 design_docs §9–§12）**；Supabase 暖快取（index）、pCloud 冷儲存。**不用 Postgres**
3. **資料層 = 移植 `finflow_ai` 的抓取/正規化/rate-limit 層**，落地改寫成 parquet sink（見 §4.2）
4. **Universe = 全部**：美股 Nasdaq100 全部 + 台股 top200 + crypto top50（→ 全市場規模讓 rate limiter 變成必要，正好複用 finflow）
5. **LLM = Bot 全走 Gemini**；Claude 深度分析 = Web 報告底部可複製 prompt，使用者自行決定
6. **每日成本上限 = 可在 `.env` 設定**（`DAILY_COST_LIMIT_TWD`，仿 finflow `storage_budget_gb` 模式）
7. **Discord = 互動式 bot**（slash command / @mention 觸發，可即時查詢）
8. **Web Report 認證 = Cloudflare Access**；MVP 先本機 localhost 開發，對外時經 Cloudflare Tunnel + Access
9. **新聞權威來源 = 鉅亨網 cnyes + Yahoo 股市**（fact 層）；PTT/Dcard 僅情緒層（見 §4.3）
10. **8:30 可靠性 = 本機跑 + 強化 catch-up**；**不加** always-on 外部觸發
11. **finflow 移植 = 複製檔案進本 repo 改寫**（非 import 依賴），移植時審查既有 bug、避免兩專案耦合

---

## 3. 系統架構

### 3.1 元件總覽

```text
┌────────────────────────────────────────────────────────────┐
│                  Windows 本機 + Docker                       │
│                                                              │
│  Scheduler ──┐  每日排程 / catch-up                          │
│              ▼                                               │
│  ┌─────────────────── Backend (FastAPI) ──────────────────┐ │
│  │  Data Layer (移植 finflow_ai 抓取層)                   │ │
│  │   loaders: yfinance / twse / tpex / finmind / coingecko│ │
│  │   + rate_limiter (Redis token bucket, per provider)    │ │
│  │        │ (PriceRow/ChipRow 正規化)                      │ │
│  │        ▼                                               │ │
│  │   local parquet (SSOT, 熱資料) ──► Processor (features)│ │
│  │        │                          │                    │ │
│  │        │                          ▼                    │ │
│  │        │                   Gemini (摘要/分析)          │ │
│  │        │                          │                    │ │
│  │        │                          ▼                    │ │
│  │        │                   Guardrail (驗證 claim 來源) │ │
│  │        │                          │                    │ │
│  │        │                          ▼                    │ │
│  │        │              Report Builder                   │ │
│  │        │           md / json / web / **copy-for-AI**   │ │
│  │        │              (copy-for-AI = 給 Claude 的 prompt)│ │
│  │        ▼                          ▼                    │ │
│  │   storage_monitor          Web Report Page (Jinja2 SSR)│ │
│  │   (env 預算/容量)                                      │ │
│  └────────────────────────┬───────────────────────────────┘ │
│  Discord Bot ─────────────┘ (互動查詢 + 推摘要 + 連結)       │
│  Redis (cache / rate-limit state / job state)               │
│  cloudflared (Tunnel → report.yourdomain.com，後段接)        │
└──────────────┬───────────────────────┬─────────────────────┘
               │                       │
          Supabase                  pCloud 2TB
      (warm: index/摘要)        (cold: 備份/快照，backup/restore)
```

### 3.2 元件職責

| 元件 | 職責 | 來源 |
|------|------|------|
| **Data Layer** | 抓原始資料、rate limit、正規化 PriceRow/ChipRow、寫入 **local parquet** | **移植 finflow_ai 抓取層**（見 §4.2）|
| **Processor** | Postgres 資料 → features（技術/籌碼/跨市場）；不做 AI 分析 | design §15 |
| **Gemini Client** | features JSON → 結構化分析（唯一系統內 LLM） | design §16/§17 |
| **Guardrail** | 驗證 claim 來源、擋禁用詞、標示資料限制 | design §18 + §4.3 |
| **Report Builder** | 從結構化分析 render md/json/web/copy-for-AI | design §19/§8 |
| **Copy-for-AI** | 產生**給 Claude 的深度分析 prompt**，Web 報告底部可一鍵複製 | design §8 |
| **Web Report Page** | 完整報告、歷史、下載（Jinja2 SSR），經 Cloudflare Tunnel 對外 | design §7 |
| **Discord Bot** | 互動式：即時查詢、推摘要+連結、anti-spam | design §21 |
| **Storage / Supabase / pCloud** | local parquet SSOT、Supabase 暖快取(index)、pCloud 冷備份、容量預算監控 | **移植 finflow_ai**（見 §4.2）|

---

## 4. 關鍵設計細節

### 4.1 LLM 層：分層供應商 + 交叉查證（憲章 2.0.0 Principle I、[specs/022-llm-tiering]）

LLM 分兩層，職責分離。**召回層負責「找得到」，決策層負責「信不信」。**

```
[ features JSON (本地 parquet, 零 LLM) ]──┐
[ 回測校準 calibration ]─────────────────┼──> Claude Opus 5 ──> BriefResult
[ Gemini facts pack + source URLs ]──────┘    ├ web_fetch  (開召回層引用的 URL 查證)
        ↑ 廣度召回，標記為「待查證」            ├ web_search (補漏 / 交叉比對)
                                              └ structured outputs（單次呼叫）
```

| 層 | 供應商 | 職責 | 明確禁止 |
|---|---|---|---|
| 廣度召回 | Gemini + `google_search` | 產出帶 source URL 的事實線索 | 不做分析、方向判斷、選股 |
| 決策 + 查證 | Claude（可設定，預設 `claude-opus-5`） | 推理、選股、報告生成、逐條查證 | 不無條件採信召回層 |

**為什麼要查證**：Gemini 的 grounding 會產生幻覺——錯置日期、把分析評論當成新聞、引用內容農場。
決策層若無條件採信，等於把幻覺洗成「有來源」的假事實。`web_fetch` 只能抓取**已出現在對話裡的
URL**，因此決策層只能去開召回層真的引用過的連結，無法自己生一個 URL，天然形成稽核閉環。
查證結果落地成 `fact_checks`，讓「召回層有沒有在胡說」成為每日可量測的數字。

```python
# backend/ai/llm_client.py
class DecisionLLM(Protocol):
    def draft_brief(self, features, facts, calibration) -> tuple[BriefDraft, Usage]: ...
    def answer_question(self, static_block, variable_block, facts) -> tuple[str, Usage]: ...

def get_decision_llm() -> DecisionLLM:   # 依 settings.llm_decision_provider 分派
```

**降級路徑**：決策層失敗（配額/過載/refusal）時退回 Gemini 既有的兩段式 grounded 晨報，
晨報是無人值守的每日排程，不得因換供應商而變成可能整份失敗。

**Claude 的另一個角色（不變）**：Report Builder 仍產出「給其他 AI 的分析包」（design §8.3）放在
Web 報告底部，供使用者手動複製貼到任何 AI 做深度分析。那是離線流程，與 serving 無關。

`.env`：`GEMINI_API_KEY`、`ANTHROPIC_API_KEY`、`LLM_DECISION_PROVIDER`、`CLAUDE_MODEL_DECISION`、
`DAILY_COST_LIMIT_TWD` / `MONTHLY_COST_LIMIT_TWD`（全站 LLM 成本上限，晨報優先於問答）。

### 4.2 資料層：移植 finflow_ai 的抓取層，落地用 parquet（核心策略）

`~/Documents/finflow_ai/apps/backend/app` 有實戰驗證的資料層。finflow 把它分成兩塊，我們**只搬第一塊**：

```text
✅ 搬：抓取/正規化/rate-limit 層 (storage-agnostic，回傳 PriceRow/ChipRow dataclass)
❌ 不搬：Postgres 落地 (models / ingest 寫 DB) → 我們改寫成 parquet sink
```

儲存沿用 design_docs 的三層（**parquet，非 Postgres**）：

```text
hot  = local parquet (SSOT)        design_docs §9.3 layout：storage/local_parquet/{market}/{symbol}.parquet
warm = Supabase (index/摘要快取)     只存 metadata / report_index，不存大資料
cold = pCloud (備份/快照)
```

> `/scan` 全 universe（~350+ 檔）改用 pandas/pyarrow 讀 features parquet 過濾 —— 每日批次、非高併發，parquet 足夠。

**移植方式**：把相關檔**複製進本 repo 改寫**（非 import finflow），移植時 **(a) 審查既有 bug 再用、(b) 切斷對 finflow Postgres models 的相依**，避免兩專案耦合。

| finflow 檔案 | 搬什麼 | 改什麼 |
|--------------|--------|--------|
| `services/rate_limiter.py` | 整支照搬（Redis token bucket，per-provider）| 改 redis key prefix |
| `services/finmind.py` | 整支照搬（tenacity retry + 正規化）| 無（回 dataclass，與儲存無關）|
| `services/twse.py` | 整支照搬（TWSE/TPEx 價＋籌碼正規化 + `is_valid()` 品質檢查）| 無 |
| `tasks/ingest.py` | 參考抓取流程 | **落地改寫**：PriceRow/ChipRow → 寫 parquet（取代寫 Postgres）|
| `services/storage_monitor.py` | 容量監控框架（footprint vs budget 分級）| **改測 parquet 目錄大小**（拿掉 `pg_database_size`），env `LOCAL_STORAGE_BUDGET_GB` |
| `services/retention.py` + `tasks/retention_cleanup.py` | 保留/清理策略框架 | **改寫成檔案級 eviction**（design §10.4 順序），非 DB row 刪除 |
| `services/pcloud.py` + `tasks/pcloud_backup.py`/`pcloud_restore.py` | 整支照搬（pCloud 備份/回補）| 路徑指向 parquet/reports，對應 design §11/§23 |
| `services/supabase_client.py` + `supabase_publish.py` | 整支照搬（service_role 寫、anon 讀）| schema 對齊 design §22 |
| `config.py` 模式 | pydantic-settings 模式照搬 | 拿掉 `database_url`，加 `DAILY_COST_LIMIT_TWD` |

移植時要沿用的 finflow 教訓（其 CLAUDE.md / 程式註解）：
- **FinMind 免費版 600 req/hr** → 必過 rate_limiter，免費 tier 只能 per-stock 抓不能全市場。
- **TPEx daily endpoint 不支援歷史**（永遠回今日）→ 歷史 backfill 改走 FinMind 個股級。
- **民國年/西元年、volume 單位（股 vs 千股）**各來源不同 → 已在 loader 正規化。
- **Future leakage**：籌碼/財報 feature index 用「公布日」不是「報告期間」。
- **時區全系統 `Asia/Taipei`**。
- ⚠️ **既有 bug 審查**：finflow 是進行中的專案，照搬前先看該檔對應的 `PROGRESS.md` Known Issues，不要把未修的 bug 一起搬進來。

### 4.3 新聞分層策略（回應「PTT/Dcard」）

新聞是最容易讓 AI 產生無來源內容的地方，**分兩層、嚴格區分可信度**：

| 層 | 來源 | 在分析中的角色 | guardrail 規則 |
|----|------|---------------|----------------|
| **權威新聞**（可當 fact 引用）| 鉅亨網 cnyes RSS、Yahoo 奇摩股市、經濟日報、工商時報、MoneyDJ、中央社 | 事實來源，必附 source/date/url | News Citation Guard：缺 url 不可當事實 |
| **論壇情緒**（只當 sentiment，**不可當事實**）| PTT Stock 板、Dcard 股票板 | 只能描述為「網路討論熱度/情緒」，明確標示非事實 | 強制標 `claim_type=inference` 且註明「來自論壇討論，非查證事實」|

→ 這樣既納入你要的 PTT/Dcard 情緒訊號，又不違反「不捏造、新聞要有來源」原則。MVP 先接 1–2 個權威 RSS + PTT Stock 板，逐步擴充。

### 4.4 結構化輸出 + Guardrail

模型直接輸出結構化 JSON，每個 claim 標類型與來源；md/web/copy-for-ai 全從這份 JSON render。

```python
# backend/ai/schemas.py
class Claim(BaseModel):
    text: str
    claim_type: Literal["fact", "calculation", "inference", "limitation"]
    source_ref: str | None   # 指向 input JSON 欄位 / news url；inference/論壇可為 None 但需註明

class ReportSection(BaseModel):
    section: str             # technical / chip / news / intermarket ...
    claims: list[Claim]

class AnalysisResult(BaseModel):
    summary: str
    sections: list[ReportSection]
    data_as_of: date
    sources: list[str]
```

Guardrail 主驗證 `claim.source_ref` 是否存在於 input；論壇來源強制標 inference；禁用詞黑名單當第二道網；失敗走 design §30.4（擋掉、改附原始資料摘要）。

---

## 5. 建構路線（垂直切片優先）

| 里程碑 | 內容 | 驗收 |
|--------|------|------|
| **M0** | repo 骨架 + docker-compose（backend + redis，**無 postgres**）+ FastAPI `/health` | `/health` 回 200 |
| **M1** | **黃金路徑垂直切片**：yfinance 抓美股指數+BTC → intermarket features → Gemini 結構化摘要 → md/json → Web 頁面 → 底部 Copy-for-Claude prompt。先用簡單 parquet，先不碰全 universe / scheduler / guardrail | `POST /brief/morning` 產生報告，瀏覽器看得到完整頁面、可複製給 Claude 的 prompt |
| **M2** | **移植 finflow 抓取層**：rate_limiter + finmind/twse/tpex/coingecko loaders（複製改寫、審 bug、解耦）+ 落地 **parquet sink** + 全 universe ingest（Nasdaq100/台股 top200/crypto top50）+ storage_monitor（parquet 目錄 vs env 預算）| 全 universe 價/籌碼落 parquet，`storage_monitor` 回報 footprint vs budget |
| **M3** | Scheduler + 強化 catch-up（啟動補產生/補推播）| 跨過排程時間重啟，catch-up 補出晨報 |
| **M4** | Discord 互動 bot：即時查詢（分析個股/找強股）、推摘要+連結、anti-spam、`DAILY_COST_LIMIT_TWD` budget guard | 測試頻道 @bot 查詢能回；每日成本上限生效 |
| **M5** | Guardrail（結構化驗證 + 論壇 inference 標示 + 禁用詞網）+ 新聞分層（cnyes + Yahoo 權威 RSS + PTT 情緒）| 餵假造輸出/論壇當事實能被擋 |
| **M6** | 持久層收尾（移植 finflow Supabase publish + pCloud backup/restore）+ **Cloudflare Tunnel + Access 對外** | report_index 寫得進 Supabase、報告備份上 pCloud、外網經 Cloudflare Access 認證後能開報告 |
| **M7** | 完整檔案級保留/清理（改寫 finflow retention）+ pCloud 按需回補 | 超預算按序清理且不刪當日晨報；缺檔能從 pCloud 回補 |

> 不在範圍：Phase 2（漂亮 Web UI / PDF）、Phase 3（回測 / vectorbt）。

---

## 6. .env（草案，對齊 finflow_ai）

> 結構沿用 `finflow_ai/.env`。標 **♻️ 複製自 finflow** 的，憑證在 `~/Documents/finflow_ai/.env` 已建好可直接搬。
> ⚠️ **資源去衝突**：pCloud / Supabase / Cloudflare 雖共用帳號，但必須用**不同命名空間**避免蓋掉 finflow 的資料 ——
> pCloud 用不同 `REMOTE_ROOT`、Supabase 用本專案自己的表（design §22，表名不同即可同 project）、Cloudflare 用不同 Public Hostname。

```text
# ── App ──
APP_ENV=development
TZ=Asia/Taipei                   # ♻️ finflow
LOG_LEVEL=info

# ── LLM（本專案新增；finflow 用 Groq，我們用 Gemini）──
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
DAILY_COST_LIMIT_TWD=30          # 每日 Gemini 成本上限，可調

# ── Discord（互動 bot，本專案新增）──
DISCORD_TOKEN=
DISCORD_GUILD_ID=
DISCORD_CHANNEL_ID=
DISCORD_ALLOWED_USER_IDS=        # 逗號分隔，限家庭成員

# ── 資料源 ──
FINMIND_TOKEN=                   # ♻️ finflow（已建）
ENABLE_TWSE=true                 # ♻️ finflow toggle 模式
ENABLE_TPEX=true
ENABLE_FINMIND=true
ENABLE_YAHOO=true

# ── 儲存（local parquet，無 Postgres）──
LOCAL_STORAGE_PATH=/app/storage
LOCAL_STORAGE_BUDGET_GB=10       # 可調，對應 storage_monitor
REDIS_URL=redis://redis:6379/0   # ♻️ finflow

# ── Supabase（暖層 index；♻️ 複製自 finflow，但本專案用自己的表）──
SUPABASE_URL=                    # ♻️ finflow（同 project 可，表名不同）
SUPABASE_ANON_KEY=               # ♻️ finflow
SUPABASE_SERVICE_ROLE_KEY=       # ♻️ finflow

# ── pCloud（冷儲存；♻️ 複製自 finflow，但用不同 REMOTE_ROOT）──
PCLOUD_CLIENT_ID=                # ♻️ finflow
PCLOUD_CLIENT_SECRET=            # ♻️ finflow
PCLOUD_ACCESS_TOKEN=            # ♻️ finflow
PCLOUD_REGION=us                # ♻️ finflow
PCLOUD_REMOTE_ROOT=/AI-Market-Research   # ⚠️ 與 finflow 不同 root，勿共用

# ── Web / Cloudflare（認證走 Cloudflare Access）──
CLOUDFLARE_TUNNEL_TOKEN=         # ♻️ finflow 帳號；建議本專案開新 tunnel/hostname
PUBLIC_HOSTNAME=report.your-domain.com   # ⚠️ 與 finflow 的 hostname 不同
PUBLIC_REPORT_BASE_URL=https://report.your-domain.com
# 認證改用 Cloudflare Access（家庭成員 email allowlist），不需 WEB_REPORT_USERNAME/PASSWORD

# ── 排程 ──
SCHEDULE_TZ=Asia/Taipei          # ♻️ finflow
MORNING_REPORT_TIME=08:30
```

---

## 7. 開放問題（已拍板）

- [x] **Web Report 認證** → **Cloudflare Access**（家庭成員 email allowlist）
- [x] **權威新聞來源** → **鉅亨網 cnyes + Yahoo 股市**（fact 層）；PTT/Dcard 僅情緒層
- [x] **8:30 可靠性** → **本機 + catch-up**，不加 always-on 外部觸發
- [x] **Postgres vs parquet** → **嚴守 design_docs 的 parquet 路線**（不用 Postgres）
- [x] **finflow 移植方式** → **複製檔案進本 repo 改寫**，審查既有 bug、切斷對 finflow 的相依避免耦合

---

## 7.1 工具定位（使用者補充，定錨用）

本工具是一個供家庭內部使用的 AI 多市場研究助理。
它不是自動交易系統，也不是保證獲利的選股機器，而是用來協助使用者整理市場資訊、分析股票與加密貨幣、理解新聞事件、觀察籌碼變化，並判斷不同市場之間可能產生的連動影響。
本工具主要透過 Discord 使用，讓家人可以用自然語言詢問市場問題，例如：
找最近比較強的台股
分析 2330 最近強不強
幫我看 TSLA 最近怎麼了
今天美股大跌，明天台股要注意什麼？
BTC 大跌會不會影響美股風險情緒？
最近 AI 概念股有什麼新聞？
找技術面轉強、籌碼也改善的股票
幫我整理今天台股風險
本工具的角色是：
資料整理員 + 市場研究助理 + AI 分析師 + 跨市場觀察員
而不是：
自動交易機器人
保證獲利系統
精準買賣訊號產生器
無資料來源的投資建議產生器

---

## 8. 與 design_docs.md 的對照索引

prompt §17｜Markdown 結構 §19.2｜Copy-for-AI 模板 §8.3｜Discord 格式 §6.3/§21.2｜Supabase schema §22｜目錄結構 §28｜docker-compose §26｜.env §27｜排程 §13｜儲存 §10–§12/§23。

**finflow_ai 參考路徑**：`~/Documents/finflow_ai/apps/backend/app/{services,tasks,models}/`、`CLAUDE.md`（坑與慣例）、`config.py`（env 模式）。
