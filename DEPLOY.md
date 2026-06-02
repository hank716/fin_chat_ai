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
| `scheduler` | 每日台股交易日 08:30 觸發晨報 + 啟動 catch-up |
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
# 回補台股 watchlist 近 90 交易日 價/籌碼/融資券（約 1~2 分鐘，FinMind rate limit）
docker exec ai-market-backend python -m data_sources.backfill_tw 90

# 立刻產一份晨報（會抓美股/加密/台股、跑 AI、存檔、推 Discord、備份 pCloud、寫 Supabase）
curl -s -X POST "localhost:8000/brief/morning"

# 瀏覽器開 https://www.hank-finflow.com/  （Cloudflare Access 登入後看歷史列表）
```

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

## 8. 從機器 A 正式搬到機器 B（切換）

1. 機器 B：完成 §2–§5，確認 `docker compose ps` 六個服務 OK、Discord bot 上線、網址可開。
2. **機器 A：停掉服務**（避免雙跑衝突）：
   ```bash
   cd ~/Documents/fin_chat_ai && docker compose down
   ```
   （`down` 不加 `-v`，保留 redis volume；本機 `/storage` 也會留著可日後還原。）
3. 之後機器 B 就是唯一在跑的節點。

---

## 9. 常見問題

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
