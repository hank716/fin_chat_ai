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
    gemini_model: str = "gemini-flash-latest"
    daily_cost_limit_twd: int = 30

    # ── Discord ──
    discord_token: str = ""
    discord_guild_id: str = ""
    discord_channel_id: str = ""
    discord_allowed_user_ids: str = ""

    # ── 資料源 ──
    finmind_token: str = ""
    enable_twse: bool = True
    enable_tpex: bool = True
    enable_finmind: bool = True
    enable_yahoo: bool = True

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
