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
    daily_cost_limit_twd: int = 30                    # 每日全站總花費上限（晨報+問答）
    monthly_cost_limit_twd: int = 600                 # 每月全站總花費上限（對齊後台預算）

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
    enable_twse: bool = True
    enable_tpex: bool = True
    enable_finmind: bool = True
    enable_yahoo: bool = True

    # ── 基本面磁碟快取 TTL（落地後晨報大多讀磁碟，只補過期→省 FinMind 額度）──
    fundamentals_revenue_ttl_days: int = 7       # 月營收：月更，最多每週重抓
    fundamentals_financials_ttl_days: int = 30   # 季財報：季更，最多每月重抓

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

    # ── 排程 ──
    schedule_tz: str = "Asia/Taipei"
    morning_report_time: str = "08:30"


settings = Settings()
