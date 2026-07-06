---
name: optimization-env-constraints
description: fin_chat_ai 優化執行的本機環境約束（venv、redis 阻塞、資料只有 3 個月深度）
metadata: 
  node_type: memory
  type: project
  originSessionId: 1c0818f7-320a-473c-8f0a-10171df5022f
---

執行 `OPTIMIZATION_PLAN.md`（Fable5 規劃、分批執行）時發現的本機約束，直接影響能做哪些 WP：

- **測試環境**：專案平常跑在 Docker，本機無 pandas/sklearn。已建 `.venv`（gitignored）裝
  pandas/numpy/scikit-learn/pyarrow/pydantic-settings/pytest/httpx/redis/tenacity。跑測試/腳本一律
  `LOCAL_STORAGE_PATH="$PWD/storage" PYTHONPATH=backend .venv/bin/python ...`。
  universe 快取本機路徑差異：需 symlink `backend/configs/universe/*.json` → repo-root `configs/universe/`。
- **redis 阻塞**：`data_sources.rate_limiter` 硬依賴 redis，本機無 redis 且 docker 未啟動 → **所有 live
  FinMind/TAIFEX 抓取直接失敗**（`Error -2 connecting to redis`）。要離線打 FinMind 可 monkeypatch
  `rate_limiter.acquire=lambda *a,**k:None`（僅診斷用）。這擋住 WP1.1/1.2/1.3、history_crawl。
- **資料深度只有 ~3 個月**（2026-03～06，每檔 ~62 列），非計畫假設的 2 年。h20 walk-forward 只切得出
  ~2 fold、meta h20 退單次切分 → **h20 統計力不足、A/B 不可歸因**。做 Phase 1/2 準確度改動前應先把
  history_crawl 跑到 ~2 年（需 redis + FinMind 額度 + 數小時）。

已完成 9/13 WP（Phase 0 全部 + 3.1/3.2/3.4/3.5/1.4），59 測試綠，分支 `feat/optimization-phase0`。
剩 WP3.3（待規格決策）、WP1.1–1.3 與 2.x（阻塞於上述 redis/資料深度）。詳見 [[fin-chat-ai-project]]、
`EVAL_BASELINE.md`、`SURVIVORSHIP_AUDIT.md`。
