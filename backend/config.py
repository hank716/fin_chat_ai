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

    # ── LLM 廣度召回層（Gemini + google_search；憲章 2.0.0 Principle I）──
    # 此層只產「帶 source URL 的待查證線索」，不做分析/選股——那是決策層的事。
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"        # 泛用/相容用預設
    gemini_model_brief: str = "gemini-pro-latest"     # 降級路徑的晨報模型（決策層失效時才用）
    gemini_model_qa: str = "gemini-flash-latest"      # 檢索/降級問答用 Flash latest（快又省）
    # 意圖分類用最便宜檔：問答前先用它過濾掉「與財務無關」的閒聊，省下大 prompt 與 grounding。
    # 刻意**不**改用決策層模型——這是 trivial gate、fail-open、token 極少，用 Opus 是純浪費。
    gemini_model_classifier: str = "gemini-flash-lite-latest"
    enable_intent_filter: bool = True                 # 啟用意圖分類器（非財務問題直接婉拒）
    # 每日全站總花費上限。⚠️ 它**只約束 /ask**——晨報刻意不受 check_budget() 攔截，
    # 走 tracker.brief_should_degrade() 的降級路徑。語意是「一篇晨報 + 若干問答」，
    # 所以必須大於單篇晨報成本（實測 NT$26–39），否則對最大宗支出完全失效。
    daily_cost_limit_twd: int = 45
    # 每月全站總花費上限。2026-08-12 盤點：近四篇單日晨報均值 NT$32.5、月約 20.5 個交易日
    # → 投影 NT$666/月，600 會讓每月最後約 4 篇晨報全部降級（無外部事件、無查證）。
    # 提到 800 是「吃下成本、不要月底品質懸崖」的取捨（憲章 II 同步修訂）。
    monthly_cost_limit_twd: int = 800
    # 月底保留給降級的緩衝：月餘額低於此值時晨報改跑節儉模式。
    # 刻意與 daily_cost_limit_twd 解耦——兩者語意無關（一個是 /ask 閘門、一個是降級門檻），
    # 共用一個旋鈕會讓調整日上限意外改變晨報降級時機。
    brief_degrade_reserve_twd: int = 45

    # ── LLM 決策 + 查證層（spec 022-llm-tiering）──
    # 吃 features + facts pack + 校準做推理選股，並用 web_fetch 逐條查證召回層引用的 URL。
    anthropic_api_key: str = ""
    llm_decision_provider: str = "anthropic"          # anthropic | gemini（gemini＝降級/A-B 對照）
    claude_model_decision: str = "claude-opus-5"      # 晨報決策（品質優先，晨報是產品本體）
    claude_model_chat: str = "claude-opus-5"          # 問答；用量稀疏，需省時可降 claude-sonnet-5
    # 問答 thinking 深度 low|medium|high|xhigh|max。2026-08-12 從 high 降到 medium：
    # 原本問答（次要、用量稀疏、且已有當日晨報 JSON 當上下文）跑得比晨報（產品本體）還深，
    # 檔位配置與「晨報 > 問答」的預算優先序相反。省下的額度回流給晨報與查證。
    claude_effort: str = "medium"
    # 晨報獨立一檔（2026-08-07 拆出）。**這是輸出端的成本主閥**：thinking token 以 output 費率
    # 計價（Opus 5 $25/MTok），effort=high 實測跑出 output 32,236 tokens ≈ NT$25.8，佔單篇
    # 決策成本 70%。39a7d03 曾記錄「effort 不是主因」——那是在 prompt cache 開啟**之前**、
    # 且兩次比較的 facts 量不同的混淆結論；input 被壓平後 effort 才第一次成為主導變數。
    claude_brief_effort: str = "medium"               # 晨報：長輸出、無人值守，用 medium 換成本
    claude_max_tokens: int = 32000                    # thinking + 回答**合計**上限；必須 streaming
    # 連網查證工具用量上限＝成本閘門（憲章 II）。沒有上限等於把每日成本交給模型自由心證。
    # ⚠️ 每多一次 fetch，成本不只是「多一頁內容」——server-side 工具迴圈在 API 內部進行，
    # **每一輪都會把累積的對話重送一次**。features JSON 約 55k tokens，max_uses=12 就代表
    # 那 55k 最多被重送 12 次。2026-08-06 實測：12 次上限跑出 NT$73.83/篇（≈NT$1,550/月）。
    # 2026-08-07 再降一階（5→3 / 3→2）：實測 8/7 那篇 7 條 fact_checks **全部 unverifiable**、
    # 8/6 那篇 6 條有 4 條 contradicted——這筆支出目前買到的是「證明線索不可信」，有價值但
    # 不值得付到 5 次。真正該修的是召回層的 URL 品質，不是多買幾次 fetch。
    claude_brief_fetch_uses: int = 3                  # 晨報：查證用 web_fetch 次數上限
    claude_brief_search_uses: int = 2                 # 晨報：補漏用 web_search 次數上限
    claude_chat_fetch_uses: int = 4                   # 問答：互動情境對延遲敏感，壓低
    claude_chat_search_uses: int = 2
    claude_fetch_max_content_tokens: int = 3000       # 單頁擷取上限（防長文把 input 撐爆）
    # pause_turn 續跑上限（防無窮迴圈），同時也是「整份晨報壓在單次呼叫」的實際旋鈕：
    # 每多一輪就多一次完整輸出 + 一次前綴重讀。調低的風險是查證做到一半被切斷，
    # 靠報告 JSON 的 `cost.tokens.rounds` 監控：連續多篇頂到上限就代表被切，再調回。
    claude_max_continuations: int = 2
    # ⚠️ 問答預設關閉是刻意的：Anthropic 快取寫入付 1.25×(5m)/2×(1h)、讀取才 0.1×，
    # 5m 需 2 次、1h 需 3 次以上讀取才回本。chat 用量稀疏時每題都是「寫入後未被讀取就過期」
    # ＝純虧。只有觀察到「同一份晨報 5 分鐘內被問 ≥2 題」才值得開。
    enable_claude_prompt_cache: bool = False
    # 晨報則**相反、預設開**：server-side 工具迴圈在單一請求內把同一個 ~55k 前綴重讀十幾次，
    # 這正是 prompt caching 最划算的場景（寫一次 1.25×，之後每輪讀 0.1×）。
    # 同一個機制、相反的結論，差別只在「同一份前綴會被讀幾次」。
    enable_claude_brief_prompt_cache: bool = True
    claude_cache_ttl: str = "5m"                      # 5m | 1h
    enable_facts_pack: bool = True                    # 關閉＝退回純 Gemini 舊路徑

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
    # Google Finance 個股頁新聞作第二來源（與 FinMind 並行、去重合併；無 API 靠爬 HTML，預設關灰度上線）
    enable_google_finance_news: bool = False

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
