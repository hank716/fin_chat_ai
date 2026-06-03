# 部署 / 搬機指南（push → clone → Docker 長期運作）

本文件說明如何把 `fin_chat_ai` 從目前這台（機器 A）推上 GitHub，clone 到另一台（機器 B），
用 Docker 長期運作。

> ⚠️ **最重要原則：同一時間只能有「一台」在跑 `bot` / `scheduler` / `cloudflared`。**
> 這三個共用憑證（Discord token、Cloudflare tunnel）。兩台同時跑會：Discord 重複回覆、
> 每日晨報重複產生、同一個對外網址出現兩個來源互搶。**搬機 = 新機起好、確認 OK 後、把舊機停掉。**

---

## 0. 兩台都要先具備

- **Docker** + **Docker Compose v2**
  - Linux：安裝 Docker Engine（`docker`、`docker compose`）。建議設定開機自啟：`sudo systemctl enable --now docker`。
  - Windows / macOS：安裝 **Docker Desktop**，並在設定開啟「Start Docker Desktop when you log in」。
- **git**
- 能存取私有 repo `github.com/hank716/fin_chat_ai`（見 §2 認證）

---

## 1. 機器 A：把程式碼推上 GitHub

`.env`（機密）與 `/storage`（本機資料）已被 `.gitignore` 排除，**不會、也不該進 git**。

```bash
cd ~/Documents/fin_chat_ai
git status                # 確認沒有要漏掉的檔案
git push origin main      # 推到 GitHub
```

> 目前本機可能領先遠端很多 commit，第一次 push 就會全部上去。

---

## 2. 機器 B：clone 私有 repo

先在機器 B 設定 GitHub 認證（擇一）：

- **GitHub CLI（最簡單）**：`gh auth login` 後 `gh repo clone hank716/fin_chat_ai`
- **HTTPS + Personal Access Token**：clone 時帳號填 GitHub 帳號、密碼貼 PAT
  （GitHub → Settings → Developer settings → Personal access tokens，給 `repo` 權限）
- **SSH**：把機器 B 的 public key 加到 GitHub，然後
  `git clone git@github.com:hank716/fin_chat_ai.git`

```bash
cd ~/Documents      # 或任何你想放的位置
git clone https://github.com/hank716/fin_chat_ai.git
cd fin_chat_ai
```

---

## 3. 機器 B：放入 `.env`（機密，需手動帶過去）

`.env` 不在 git 裡，必須**用安全方式**從機器 A 帶到機器 B（USB、加密傳輸、密碼管理器…，
**不要**貼到聊天室或 commit 進 git）。

```bash
# 在機器 A 看一下要帶哪些 key（不要外流值）
cat .env

# 把整個 .env 複製到機器 B 的 repo 根目錄
#   機器B: ~/Documents/fin_chat_ai/.env
```

`.env` 必填項（對照 `.env.example`）：

| 類別 | 變數 |
|---|---|
| LLM | `GEMINI_API_KEY`、`GEMINI_MODEL_BRIEF`、`GEMINI_MODEL_QA`、`DAILY_COST_LIMIT_TWD`、`MONTHLY_COST_LIMIT_TWD` |
| 管理 | `ADMIN_TOKEN`（保護 `/admin/*` 成本校準端點；留空＝端點停用） |
| 排程 | `SCHEDULE_TZ`、`REPORT_TIMES`（逗號分隔 HH:MM，控制每日自動產報告的時間點） |
| 資料源 | `FINMIND_TOKEN` |
| Discord | `DISCORD_TOKEN`、`DISCORD_GUILD_ID`、`DISCORD_DAILY_REPORT_CHANNEL_ID`、`DISCORD_JAY_CHAT_CHANNEL_ID`、`DISCORD_HANK_CHAT_CHANNEL_ID`、`DISCORD_JAY_USER_ID`、`DISCORD_HANK_USER_ID` |
| Supabase | `SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`（report_index 表沿用同一個，不用重建） |
| pCloud | `PCLOUD_ACCESS_TOKEN`、`PCLOUD_REMOTE_ROOT`（沿用 `/AI-Market-Research`） |
| Cloudflare | `CLOUDFLARE_TUNNEL_TOKEN`、`PUBLIC_HOSTNAME`、`PUBLIC_REPORT_BASE_URL` |

> Supabase 表、pCloud 資料夾、Cloudflare tunnel 路由都是雲端共用資源，**搬機不需重建**，
> 沿用同一份憑證即可。Cloudflare tunnel 的 ingress 已指向 `http://caddy:80`，本 repo 的
> `caddy` 服務會接上，無需改 Cloudflare 後台。

---

## 4. 機器 B：建置並啟動

```bash
cd ~/Documents/fin_chat_ai
docker compose up -d --build
```

會啟動 6 個服務：

| 服務 | 用途 |
|---|---|
| `backend` | FastAPI：產報告 / Q&A / guardrail / 成本 / Web 頁 |
| `scheduler` | 台股交易日依 `REPORT_TIMES`（預設 08:30）觸發報告 + 啟動 catch-up |
| `bot` | Discord 互動 bot（FinBot） |
| `caddy` | 反向代理（Cloudflare tunnel → `caddy:80` → `backend:8000`） |
| `cloudflared` | Cloudflare tunnel，對外 `www.hank-finflow.com` |
| `redis` | rate limiter / 成本累計 |

確認啟動狀態：

```bash
docker compose ps                       # 6 個都 Up，backend 應 healthy
curl -s localhost:8000/health           # {"status":"ok","redis":"up",...}
docker logs ai-market-bot --tail 20     # 應看到「bot 已連線：FinBot...」
docker logs ai-market-cloudflared 2>&1 | grep -i registered   # tunnel 已連線
```

---

## 5. 機器 B：初始化資料（storage 是空的）

`/storage`（parquet、報告）沒進 git，新機是空的。兩種方式擇一：

**A. 直接重新抓（推薦）**

```bash
# 回補全台股近 90 交易日 價/籌碼/融資券（TWSE/TPEx 全市場單日端點，不走 FinMind、不會被 ban）
# watchlist 自 M8 起為全台股 ~2700 檔，務必用 _market 版；勿用 backfill_tw（逐檔打 FinMind 會爆 600/hr 被封 IP）
docker exec ai-market-backend python -m data_sources.backfill_tw_market 90

# 立刻產一份晨報（會抓美股/加密/台股、跑 AI、存檔、推 Discord、備份 pCloud、寫 Supabase）
curl -s -X POST "localhost:8000/brief/morning"

# 瀏覽器開 https://www.hank-finflow.com/  （Cloudflare Access 登入後看歷史列表）
```

> 少量指定標的或 on-demand 單檔才用 FinMind 逐檔版：
> `docker exec ai-market-backend python -m data_sources.backfill_tw 90`
> 遇 FinMind IP 封鎖（403 ip banned）時它會自動等 `retry_after` 後從中斷處續跑；
> 加 `--no-wait` 則立即中止（退出碼 2），不自動等候。

> 若不想初始化時就推 Discord / 發布，可加參數：
> `curl -s -X POST "localhost:8000/brief/morning?push_discord=false&publish=false"`

**B. 不手動初始化**：直接等下一個台股交易日 08:30，scheduler 會自動產生；
若那天開機晚了，啟動時的 catch-up 會在「已過 08:30 且今日尚無報告」時補產。
（歷史舊報告會在有人開 `/report/{id}` 時自動從 pCloud 回補。）

---

## 6. 長期運作

- **開機自啟**：所有服務都設了 `restart: unless-stopped`，Docker 一啟動就會把它們拉起來。
  確保 Docker 本身開機自啟（Linux：`systemctl enable docker`；Windows/mac：Docker Desktop 設定）。
- **每日節奏**：台股交易日 08:30 自動產晨報 → Guardrail → pCloud 備份 + Supabase 索引 +
  容量清理 → Discord 推摘要。非交易日（週末 / `configs/tw_holidays.json` 假日）自動略過。
- **看狀態 / log**：
  ```bash
  docker compose ps
  docker logs ai-market-scheduler --tail 30
  docker logs ai-market-backend --tail 50
  curl -s localhost:8000/storage          # 本機容量 vs 10GB 預算
  curl -s localhost:8000/brief/status     # 今天是否交易日 / 是否已產報告
  ```
- **容量**：retention 會自動把本機報告留最近 90 篇、清掉清單外的暫存 parquet；
  也可手動 `curl -s -X POST localhost:8000/storage/retention`。

---

## 7. 更新程式（機器 B 跟上新 commit）

```bash
cd ~/Documents/fin_chat_ai
git pull origin main
docker compose up -d --build      # 只會重建有變動的 image
```

> 注意：**backend / bot / scheduler 的程式碼是「build 進 image」的**（不是掛載），
> 改完一定要 `--build` 重建才會生效。`configs/` 與 `storage/` 是掛載，改了即時生效。

---

## 8. 改 `.env` 設定（Docker 運作中要改動時）

`.env` 是**啟動時**被 Docker Compose 讀進容器當環境變數的，所以**改了 `.env` 不會自動生效，
要重啟對應服務**（不用 `--build`，因為 `.env` 不是 build 進 image 的）。

### 流程（三步）

```bash
cd ~/Documents/fin_chat_ai          # Windows：cd 到你的 repo 資料夾

# 1) 編輯 .env
#    Linux/mac：nano .env  或  vim .env
#    Windows：用記事本 / VS Code 開 .env（存檔請保持 UTF-8、不要存成 .env.txt）

# 2) 重啟「會用到那個變數」的服務（見下表）
docker compose up -d scheduler      # 例：只改了排程

# 3) 確認新值生效
docker exec ai-market-scheduler env | grep REPORT_TIMES
docker logs ai-market-scheduler --tail 5      # 應看到「scheduler 啟動：每日 …」
```

> 改 `.env` 後若懶得分辨，`docker compose up -d` 不帶服務名也行——Compose 會自動只重建/重啟
> 設定有變動的容器。**不需要 `down`**，也**不要加 `--build`**（那是給改程式碼用的）。

### 哪個變數改完要重啟哪個服務

| 改了什麼 | 重啟指令 |
|---|---|
| `REPORT_TIMES` / `SCHEDULE_TZ`（排程時間點） | `docker compose up -d scheduler` |
| `DAILY_COST_LIMIT_TWD` / `MONTHLY_COST_LIMIT_TWD` / `GEMINI_*` / `FINMIND_TOKEN` / `ADMIN_TOKEN` | `docker compose up -d backend` |
| `DISCORD_*`（頻道 / token / 使用者） | `docker compose up -d bot` |
| `CLOUDFLARE_TUNNEL_TOKEN` / `PUBLIC_*` | `docker compose up -d cloudflared caddy` |
| 不確定 | `docker compose up -d`（全部，自動只動有變的） |

### 例：新增 / 修改報告時間點

編輯 `.env` 的 `REPORT_TIMES`（逗號分隔，時區 = `SCHEDULE_TZ`）：

```ini
# 只要盤前晨報（預設）
REPORT_TIMES=08:30
# 盤前 + 台股盤後
REPORT_TIMES=08:30,14:00
# 盤前 + 台股盤後 + 美股盤前
REPORT_TIMES=08:30,14:00,21:30
```

存檔後 `docker compose up -d scheduler`。每篇報告 token 成本 ≈ NT$7.7、每月約 21 交易日，
所以 1 次/日 ≈ NT$160、2 次 ≈ NT$320、3 次 ≈ NT$480/月——加時段時記得對照 `MONTHLY_COST_LIMIT_TWD`。

### 基本面（財報）磁碟快取、焦點預抓 `PREFETCH_TIMES`、全市場慢爬 `CRAWL_TIMES`

財報是**慢變動資料**（月營收月更、季財報季更），已**落地磁碟快取**（`storage/cache/fundamentals/`，
月營收 TTL 7 天、季財報 30 天），所以晨報大多直接讀磁碟、只補過期的少數檔，FinMind 用量大降，
**不需要為了抓財報在半夜開機**。TTL 可用 `FUNDAMENTALS_REVENUE_TTL_DAYS` /
`FUNDAMENTALS_FINANCIALS_TTL_DAYS` 調整（通常不用）。

> 資料分工：**財報＝慢資料，落地快取**；**法說會 / 新聞 / PTT 等即時快訊＝不快取，由 AI（Gemini
> grounding 即時搜尋）在產報告/問答當下抓**。所以快取只負責季更的財務數字，不會讓快訊變舊。

三種暖快取方式（由快到慢、由焦點到全市場）：

```bash
# ① 焦點股預抓（快，數分鐘）：跟晨報同源，篩出當天 movers 才暖——08:30 前先暖好
docker exec ai-market-backend python -m processor.prefetch_fundamentals
#   排程版：.env 設 PREFETCH_TIMES=07:30 → 07:30 暖焦點、08:30 產報告幾乎不打 FinMind

# ② 全市場「慢爬」（焦點優先，再慢慢掃全部 2700+；可數小時～23h）
docker exec -d ai-market-backend python -m processor.prefetch_fundamentals --full         # 背景跑到完成/關機
docker exec ai-market-backend python -m processor.prefetch_fundamentals --full --minutes 50  # 本次只跑 50 分鐘
curl -s -X POST "localhost:8000/brief/prefetch?scope=full"                                # HTTP 版（背景、立刻回）
#   排程版：.env 設 CRAWL_TIMES=09:00（機器長時間開著時用；待機/省電就留空，需要時再手動跑）

# ③ 精選 watchlist / 強制重抓
docker exec ai-market-backend python -m processor.prefetch_fundamentals --watchlist
docker exec ai-market-backend python -m processor.prefetch_fundamentals --force
```

> **慢爬可重入/可續跑**：靠磁碟快取＋TTL，已抓且未過期的會被「秒跳過」，所以中途睡眠/關機都沒關係，
> 下次再跑會自動跳過已抓的、接著抓還沒抓的。全市場跑完後，整季都讀磁碟、幾乎不再打 FinMind；
> 過季後 TTL 到期再慢慢重爬。**同時只允許一個慢爬**（重複觸發會回 `already running`）。

> 若你用「待機 + 遠端喚醒」省電：`PREFETCH_TIMES=07:30` 保證晨報前暖好焦點股就夠用；`CRAWL_TIMES`
> 留空、想讓財報慢慢長滿全市場時，喚醒後手動跑一次 `--full`（背景續跑）即可。

### 校正 / 重設「本月 AI 花費」數字（首頁橫幅）

首頁橫幅與每日上限是用 redis 累計的 token 估算，和 Google 後台的真實帳單會有落差
（cache 折扣、>200k 級距、grounding、匯率、`*-latest` 別名實際版本等）。要對齊後台真實值，
兩種方式擇一：

**A. HTTP 端點（推薦，會保留月桶 TTL、回傳校準前後值）**

先在 `.env` 設好 `ADMIN_TOKEN`（保護 `/admin/*`，留空＝端點停用）：

```bash
# 產生一段隨機 token，填進 .env 的 ADMIN_TOKEN，然後重啟 backend
python -c "import secrets;print(secrets.token_urlsafe(32))"
docker compose up -d backend

# 把「當月全站累計」校準成 Google 後台的本月實際金額（例：121.15）
curl -s -X POST "localhost:8000/admin/cost/calibrate?month_total_twd=121.15" \
  -H "X-Admin-Token: <你 .env 裡的 ADMIN_TOKEN>"
# 回傳 {"month":"2026-06","before_twd":..., "after_twd":121.15, "day_total_twd":...}
```

> 未設 `ADMIN_TOKEN` → 端點回 503（fail-closed）；token 不符 → 401。對外網域走 Cloudflare
> Access 已擋一層，這個 token 再多擋一層，避免有人改你的成本計數。

**測試時不被每日上限擋（但花費仍計入）**：壓測/驗證問答時，`/ask` 帶同一個 `X-Admin-Token`
就會跳過 `DAILY_COST_LIMIT_TWD` 上限，但**成本照常累加進 redis**（不會少算）。一般 Discord
問答不帶此標頭，仍受每日上限保護。

```bash
curl -s -X POST "localhost:8000/ask" -H "Content-Type: application/json" \
  -H "X-Admin-Token: <你的 ADMIN_TOKEN>" \
  -d '{"user_id":"test","question":"台積電2330走勢?"}'
```

**B. 直接寫 redis（不經 backend；不會回傳校準前後值）**

```bash
# 看現在的桶（YYYYMM / YYYYMMDD = 你的 SCHEDULE_TZ 當下日期）
docker exec ai-market-redis redis-cli keys 'cost:*'

# 直接覆寫成後台真實金額（例：本月 121.15）
docker exec ai-market-redis redis-cli set cost:month:202606 121.15
# 重設今日累計（例如測試後想歸零）
docker exec ai-market-redis redis-cli set cost:day:20260602 0
```

> 月桶 TTL 70 天、日桶 48 小時，跨月/隔日自然汰換，平時不用手動清。重啟服務不會清掉
> redis（volume 保留）；只有 `docker compose down -v` 才會連 redis 資料一起刪。

---

## 9. 從機器 A 正式搬到機器 B（切換）

1. 機器 B：完成 §2–§5，確認 `docker compose ps` 六個服務 OK、Discord bot 上線、網址可開。
2. **機器 A：停掉服務**（避免雙跑衝突）：
   ```bash
   cd ~/Documents/fin_chat_ai && docker compose down
   ```
   （`down` 不加 `-v`，保留 redis volume；本機 `/storage` 也會留著可日後還原。）
3. 之後機器 B 就是唯一在跑的節點。

---

## 10. 常見問題

- **開網址出現一段 JSON 而不是頁面**：那是舊版；本版 `/` 是歷史列表。確認 `--build` 重建過。
- **網址 502 / 1033**：`cloudflared` 沒連上，或 `caddy` 沒起。看 `docker logs ai-market-cloudflared`、
  `docker logs ai-market-caddy`；確認 `CLOUDFLARE_TUNNEL_TOKEN` 正確。
- **Discord bot 不回**：①確認只有一台在跑 ②Developer Portal 開了 **MESSAGE CONTENT INTENT**
  ③`DISCORD_TOKEN` 是 Bot Token（3 段、約 70 字元），不是 Client Secret。
- **Discord 重複回覆 / 晨報重複**：兩台同時在跑。停掉其中一台（§8）。
- **晨報沒出來**：當天是否台股交易日？`curl localhost:8000/brief/status`；PC 8:30 是否開機
  （沒開機則靠開機後 catch-up）。
- **Gemini 429（配額用盡）/ 503（過載）**：429 會 fail-fast、503 會自動重試；查 Google 後台用量。
- **成本顯示與後台對不上**：頁面金額是 token × 官方費率的「估算」，精確以 Google 後台為準。

---

## 服務埠 / 對外

- 本機：`http://localhost:8000`（FastAPI）
- 對外：`https://www.hank-finflow.com`（Cloudflare tunnel + Access，僅允許名單內 email）
