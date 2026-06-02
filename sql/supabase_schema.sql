-- fin_chat_ai Supabase schema（M6，對齊 design_docs §22）
-- 一次性執行：Supabase Dashboard → SQL Editor → 貼上 → Run。
-- 沿用 finflow project（表已清空），本專案用自己的表名；report_id 唯一鍵供 upsert。

create table if not exists report_index (
    id uuid primary key default gen_random_uuid(),
    report_id text unique not null,           -- morning_YYYYMMDD_HHMMSS，upsert 依此
    report_type text not null,
    title text not null,
    report_date date not null,
    data_as_of date,
    market_scope text[],
    local_markdown_path text,
    local_json_path text,
    pcloud_markdown_path text,
    pcloud_json_path text,
    public_url text,
    discord_pushed boolean default false,
    created_at timestamptz default now()
);

create index if not exists report_index_report_date_idx on report_index (report_date desc);
