"""集中式設定（pydantic-settings）。

對齊 ARCHITECTURE.md §6 / .env.example。M0 只需 REDIS_URL；其餘欄位先宣告，
後續里程碑（資料源 / LLM / 儲存 / Discord）逐步使用。多數欄位給預設值，
讓尚未填的 placeholder（Gemini / Discord）不會在 M0 啟動時報錯。
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──
    app_env: str = "development"
    tz: str = "Asia/Taipei"
    log_level: str = "info"

    # ── LLM（Gemini-only）──
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"        # 泛用/相容用預設
    gemini_model_brief: str = "gemini-pro-latest"     # 每日晨報用 PRO latest（品質優先）
    gemini_model_qa: str = "gemini-flash-latest"      # 平日問答用 Flash latest（快又省）
    # 意圖分類用最便宜檔：問答前先用它過濾掉「與財務無關」的閒聊，省下大 prompt 與 grounding。
    gemini_model_classifier: str = "gemini-flash-lite-latest"
    enable_intent_filter: bool = True                 # 啟用意圖分類器（非財務問題直接婉拒）
    daily_cost_limit_twd: int = 30                    # 每日全站總花費上限（晨報+問答）
    monthly_cost_limit_twd: int = 600                 # 每月全站總花費上限（對齊後台預算）

    # ── Gemini 明確快取（cachedContents API；同日問答重用當日靜態 context 省 input token）──
    enable_gemini_explicit_cache: bool = True
    gemini_cache_ttl_seconds: int = 7200              # 明確快取 TTL（秒）；對齊「當日一份晨報」生命週期

    # ── 管理端點權杖（/admin/*；空＝停用管理端點，fail-closed）──
    admin_token: str = ""

    # ── Discord（1 guild / 3 channels / 2 users）──
    discord_token: str = ""
    discord_guild_id: str = ""
    discord_daily_report_channel_id: str = ""   # 每日晨報廣播（兩人都看）
    discord_jay_chat_channel_id: str = ""        # Jay 專屬互動頻道
    discord_hank_chat_channel_id: str = ""       # Hank 專屬互動頻道
    discord_jay_user_id: str = ""
    discord_hank_user_id: str = ""

    # ── 資料源 ──
    finmind_token: str = ""
    # FinMind 免費版每小時請求上限 600；留安全邊際設 550，rate_limiter 跨 process 用
    # Redis 計數，超過就讓慢爬退避（互動晨報在靜默窗外、獨享當小時額度，不會撞上）。0＝關閉。
    finmind_hourly_budget: int = 550
    enable_twse: bool = True
    enable_tpex: bool = True
    enable_finmind: bool = True
    enable_yahoo: bool = True

    # ── 基本面磁碟快取 TTL（落地後晨報大多讀磁碟，只補過期→省 FinMind 額度）──
    # 註：季報/月營收改走「日曆感知略過」（依申報截止日判斷有無新一期），下列 TTL 僅
    # 在快取缺少期別資訊時當保險退路用。
    fundamentals_revenue_ttl_days: int = 7       # 月營收 fallback：無月份資訊時最多每週重抓
    fundamentals_financials_ttl_days: int = 30   # 季財報 fallback：無季別資訊時最多每月重抓
    # 日曆感知略過：申報截止日後再加幾天緩衝才視為「該期已全到齊」（避免晚報公司被永久跳過）
    fundamentals_filing_buffer_days: int = 5
    # 負快取（查無財報的標的，多為 ETF/權證）長 TTL：不必每輪重探，約季度重探一次
    fundamentals_negcache_ttl_days: int = 80
    # 全市場財報慢爬「靜默窗」：此區間慢爬暫停，把 FinMind 每小時額度完整讓給晨報。
    # 預設「連動」晨報排程自動推算（見 prefetch_fundamentals._crawl_blackout_window）：
    #   start = 最早晨報活動(prefetch/report) − crawl_blackout_lead_min（FinMind 額度按小時回補，
    #           晨報前 1 小時停抓即可讓那小時額度全歸晨報）
    #   end   = 最晚晨報活動 + crawl_blackout_tail_min（容報告產製時間）
    # 只有想手動釘死時才填 fundamentals_crawl_blackout="HH:MM-HH:MM"（非空＝覆寫自動推算）。
    fundamentals_crawl_blackout: str = ""
    crawl_blackout_lead_min: int = 60
    crawl_blackout_tail_min: int = 40

    # ── 儲存（local parquet SSOT，無 Postgres）──
    local_storage_path: str = "/app/storage"
    local_storage_budget_gb: int = 10
    redis_url: str = "redis://redis:6379/0"

    # ── Supabase（暖層 index）──
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    # ── pCloud（冷儲存）──
    pcloud_client_id: str = ""
    pcloud_client_secret: str = ""
    pcloud_access_token: str = ""
    pcloud_region: str = "us"
    pcloud_remote_root: str = "/AI-Market-Research"

    # ── Web / Cloudflare ──
    cloudflare_tunnel_token: str = ""
    public_hostname: str = ""
    public_report_base_url: str = ""

    # ── 回測 / 策略自動修正（本地、零 LLM 成本）──
    # 對「過去晨報的 tw_watchlist/tw_caution 預估」做事後回測，產出校準回灌晨報 prompt，
    # 並（資料夠時）訓練本地 ML edge 模型替候選打成功機率。全程讀本機 parquet，不打外部 API。
    backtest_horizons: str = "5,20"               # 評估時間窗（交易日），逗號分隔；同時跑
    backtest_calibration_lookback: int = 60       # 校準彙整取最近幾份已到期報告
    enable_strategy_calibration: bool = True      # 把回測校準文字注入晨報 prompt（自我修正）
    enable_edge_model: bool = True                # 啟用本地 ML edge 模型（樣本不足時自動跳過）
    edge_model_min_samples: int = 150             # 訓練 edge 模型所需最少已到期樣本數
    # 回撤風險模型（方向 edge 撞效率牆，改預測「未來會不會深跌」＝波動持續性，OOS AUC 高很多）。
    enable_risk_model: bool = True                # 啟用本地回撤風險模型（與方向 edge 並存、不取代）
    risk_model_min_auc: float = 0.58              # 風險模型 OOS AUC ≥ 此值才用於排序/標記（比 edge 0.52 嚴）
    # 報酬 rank 模型（Phase 2[12]）：回歸因子中性化殘差報酬，以 rank-IC 評估、過門檻才重排偏多。
    enable_rank_model: bool = True                # 啟用本地報酬 rank 模型（殘差方向，learning-to-rank 取向）
    rank_ic_gate: float = 0.03                    # rank 模型 OOS rank-IC ≥ 此值才重排 watchlist（液態股方向多半不過＝正常）
    # Qlib 離線因子/排序（階段 3[8]）：pyqlib 跑在獨立 image，離線寫 qlib_scores/qlib_meta.json；
    # serving 端只讀 JSON、永不 import qlib。沿用 rank_ic_gate 守護（過 gate 才重排，無則回空、不動晨報）。
    enable_qlib: bool = True                       # 啟用 Qlib 方向分數（讀離線 image 產的 JSON；缺檔/未過 gate 自動不動）
    # Meta-labeling（階段 2[5]）：不拚方向、拚「這個訊號該不該下手」(triple-barrier 是否先觸目標) →
    # P(成功) 做部位 sizing/過濾。歷史回放 bootstrap + 線上 scorecard 增量；過 gate 才啟用。
    enable_meta_model: bool = True                 # 啟用 meta-labeling 模型（sizing/過濾，不重排方向）
    meta_model_min_auc: float = 0.55              # meta 模型 OOS AUC ≥ 此值才用於 sizing（同 edge 噪音帶門檻）
    # 部位 sizing：把 risk_score×conviction_score 合成 long-only 部位權重；唯有離線回測證明某方案淨贏等權
    # （best_scheme 非 None）才套用，否則退回等權（no-op）。見 strategy_calibration.backtest_sizing/sizing_plan。
    enable_sizing: bool = True                     # 啟用部位 sizing（過回測 gate 才實際加權，否則等權）
    sizing_max_weight: float = 0.30               # 單檔權重上限（避免集中）
    sizing_min_alpha: float = 0.0                 # 回測中某方案毛報酬贏等權 > 此值才採用（gate）
    sizing_max_reduction: float = 0.5             # 市場恐慌極端時最多降的總曝險比例（fear=1→曝險×(1−此值)）
    # TAIFEX 選擇權 P/C ratio（市場恐慌/避險 gauge）：橫斷面 ~0（誠實量測），真正用於市場 regime/總曝險覆蓋。
    enable_taifex: bool = True                     # 啟用 TAIFEX P/C 抓取與市場 regime 曝險覆蓋
    market_regime_min_gap: float = 0.5            # 高恐慌 vs 低恐慌 tercile 未來回撤差(%) ≥ 此值才啟用曝險覆蓋（gate）

    # ── 歷史行情慢爬（把 parquet 歷史拉長，餵大 edge 訓練集；見 data_sources.history_crawl）──
    history_crawl_target_days: int = 730          # 回溯目標（天）：預設 2 年（落在 TWSE 2024+ schema）
    history_crawl_chunk_days: int = 30            # 軌道 A 每 chunk 回補幾天（界記憶體/續跑粒度）
    history_crawl_max_minutes: float = 40         # 軌道 A 每輪時間預算（分）
    history_finmind_per_run: int = 120            # 軌道 B（上櫃價）每輪最多打幾次 FinMind（<550/h 留額度給晨報）
    history_fund_per_run: int = 400               # 軌道 C（基本面）每輪 FinMind 呼叫上限（每檔約 4 次→~100 檔/輪）

    # ── 排程 ──
    schedule_tz: str = "Asia/Taipei"
    morning_report_time: str = "08:30"
    # 與 scheduler 同源讀 env（逗號分隔 HH:MM）：供 backend 推算慢爬靜默窗，避免兩處時間失聯。
    prefetch_times: str = ""   # PREFETCH_TIMES
    report_times: str = ""     # REPORT_TIMES


settings = Settings()
