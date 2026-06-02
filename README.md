# AI 多市場研究助理 (fin_chat_ai)

家庭內部使用的 AI 多市場研究助理：每日市場晨報 → Discord 摘要 + Web Report。
資料整理員 + 市場研究助理 + AI 分析師 + 跨市場觀察員，**非**自動交易 / 保證獲利系統。

- 規格：[`design_docs.md`](design_docs.md)
- 架構決策（衝突時以此為準）：[`ARCHITECTURE.md`](ARCHITECTURE.md)

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
- [~] M6 — Supabase publish + pCloud backup + Cloudflare Access ✅ pCloud 備份+Cloudflare tunnel(caddy 反代)+Supabase report_index 發布程式完成；**待補**：在 Supabase SQL Editor 跑 `sql/supabase_schema.sql` 建表、Cloudflare Access email 政策設定
- [ ] M7 — 檔案級保留/清理 + pCloud 回補

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

## 目錄結構

```
backend/    FastAPI：api / router / data_sources / processor / ai / guardrails / reports / storage / cost
bot/        Discord 互動 bot（M4）
scheduler/  排程 + catch-up（M3）
configs/    universe / features YAML
storage/    本機 parquet / cache / reports（gitignore，runtime 產生）
```
