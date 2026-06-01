# AI 多市場研究助理 Design Docs v1.1

## 0. 文件目的

本文件定義一套供家庭內部使用的 AI 多市場研究助理。

本工具會包成 Docker，運作在本機 Windows 電腦上，並整合既有資源：

```text
pCloud API 2TB
Supabase Free Tier
Gemini API
Discord
Cloudflare 網域
Windows 本機 + Docker
```

本工具不是自動交易系統，也不執行真實下單。

它的目標是每天自動整理市場資訊，並讓家庭成員透過 Discord 與 Web Report Page 查閱市場分析報告。

---

# 1. 工具定位

## 1.1 一句話定義

本工具是一個供家庭內部使用的 AI 多市場研究助理，能根據市場資料、技術面、基本面、籌碼面、新聞事件與跨市場連動資訊，協助使用者理解美股、台股、加密貨幣與總體市場之間的關聯，找出值得進一步研究的標的與風險。

系統不執行真實交易、不保證獲利，所有數據必須來自資料源或系統計算，所有新聞必須有來源與日期，AI 不得捏造資料，也不可把推論描述成必然結果。

## 1.2 工具角色

本工具的角色是：

```text
資料整理員
市場研究助理
AI 分析師
跨市場觀察員
研究包產生器
```

不是：

```text
自動交易機器人
下單系統
保證獲利系統
精準買賣訊號產生器
無資料來源的投資建議產生器
```

---

# 2. 核心產品行為

## 2.1 每日自動市場晨報

系統每天早上 8:30，依照 Asia/Taipei 時區，自動產生一份多市場分析報告。

報告會：

```text
產生完整 Web Report Page
產生 Markdown 報告
產生 JSON 原始分析包
同步報告到 pCloud
寫入 Supabase report_index
推送 Discord 簡短摘要與 Web Report 連結
```

## 2.2 Discord 與 Web Report 的分工

Discord 的角色是：

```text
提醒 + 重點摘要 + 報告連結
```

Web Report Page 的角色是：

```text
完整市場研究報告
歷史報告瀏覽
Markdown / JSON 下載
給其他 AI 的分析包
```

Discord 不承載完整長文，只放重點與連結。

---

# 3. 核心目標

本工具要解決的問題是：

```text
市場資訊太多，而且美股、台股、加密貨幣、總經數據與新聞事件彼此會互相影響。
家人不一定有時間整理這些資訊，因此需要一個 AI 助理協助快速整理、分析、比較與提醒風險。
```

核心目標：

1. 每天早上 8:30 自動產生市場晨報
2. 分析美股、台股、加密貨幣與總體風險情緒
3. 分析技術面、基本面、籌碼面、新聞面與跨市場連動
4. 找出值得進一步研究的候選標的
5. Discord 推送簡短重點與 Web Report 連結
6. Web Report Page 提供完整報告與歷史查詢
7. Web Report 底部提供可貼給其他 AI 的分析包
8. 本機資料上限 10GB，長期資料放 pCloud
9. 本機資料不足時才從 pCloud 按需下載
10. 使用 Gemini 產生 AI 分析，但不得捏造資料或新聞

---

# 4. 既有資源整合策略

## 4.1 資源使用總覽

| 資源                  | 建議用途                                          | MVP 是否使用 |
| --------------------- | ------------------------------------------------- | ------------ |
| Windows 本機 + Docker | 主要運算與服務執行                                | 必用         |
| Discord               | 家庭成員提醒與簡短摘要入口                        | 必用         |
| Web Report Page       | 完整報告閱讀與歷史查詢                            | 必用         |
| Cloudflare 網域       | 對外提供 Web Report Page 固定網址                 | 必用         |
| Cloudflare Tunnel     | 將本機 Web Report 安全 expose 出去                | 必用         |
| Gemini API            | AI 分析、摘要、報告生成                           | 必用         |
| Supabase Free Tier    | metadata、usage logs、report index、storage index | 必用         |
| pCloud API 2TB        | 長期冷儲存、報告備份、parquet 備份                | 必用         |
| Redis                 | cache、rate limit、job state                      | 建議使用     |
| Local Parquet         | 本機熱資料與 feature dataset                      | 必用         |

## 4.2 資源定位

### Windows 本機 + Docker

本機是主要運算環境。

負責：

```text
資料抓取
資料清洗
feature 計算
AI 分析前處理
Gemini 呼叫
報告產生
Web Report Service
Discord Bot
Scheduler
Storage Manager
```

### Discord

Discord 是家庭成員的提醒與互動入口。

負責：

```text
每日晨報推送
簡短摘要
Web Report 連結
即時查詢指令
簡短查詢結果
```

### Web Report Page

Web Report Page 是完整報告入口。

負責：

```text
完整市場晨報閱讀
單股分析報告閱讀
候選標的掃描報告閱讀
歷史報告列表
Markdown / JSON 下載
Copy for Another AI
```

### Gemini API

Gemini 用於 AI 分析與報告生成。

原則：

```text
Gemini 不直接創造資料
Gemini 只根據 input JSON、資料來源與新聞來源分析
所有市場數據必須由 Data Layer 提供
所有新聞必須有 source、date、url
AI 推論必須標示為推論
```

### Supabase Free Tier

Supabase 用於小型結構化資料與索引，不存大型市場資料。

適合存：

```text
dataset_metadata
report_index
storage_objects
analysis_runs
usage_logs
query_history
cache_index
user_settings
```

不適合存：

```text
大量 OHLCV 歷史資料
大型 parquet
大量新聞全文
大型回測結果
```

### pCloud API 2TB

pCloud 作為冷儲存與備份。

適合存：

```text
長期歷史 parquet
feature dataset 備份
Markdown 報告
JSON 分析包
每日市場快照
舊報告歸檔
```

不建議直接從 pCloud 做大量運算。

正確做法：

```text
本機優先運算
本機不足才從 pCloud restore
Supabase 存 metadata 與 pCloud path
pCloud 存冷資料與備份
```

### Cloudflare 網域與 Tunnel

Cloudflare 是 MVP 必備，用於提供 Web Report Page。

用途：

```text
report.yourdomain.com
Cloudflare Tunnel 連回本機 backend
避免直接暴露家中 IP
提供家庭成員固定網址
```

---

# 5. 核心分析維度

本工具的核心分析維度為：

```text
技術面 + 基本面 + 籌碼面 + 新聞面 + 跨市場連動
```

## 5.1 技術面

技術面用來觀察價格、成交量與趨勢。

可分析：

```text
MA20 / MA50 / MA200
5D / 20D / 60D return
成交量放大或萎縮
突破、跌破、盤整、轉強、轉弱
波動度
相對強弱
短線是否過熱
大盤與個股趨勢是否一致
```

## 5.2 基本面

基本面用來理解公司營運品質與成長狀況。

可分析：

```text
月營收 YoY / MoM
季營收
EPS
毛利率
營業利益率
現金流
本益比
股價淨值比
財報優於或低於預期
營運改善或惡化跡象
```

MVP 可先做基本資訊與 placeholder，Phase 1.5 或 Phase 2 再逐步補強。

## 5.3 籌碼面

籌碼面是台股分析的重要核心能力。

可分析：

```text
外資買賣超
投信買賣超
自營商買賣超
三大法人合計買賣超
法人連續買超或賣超
外資持股比例變化
融資餘額變化
融券餘額變化
券資比
大戶持股比例
散戶持股比例
籌碼集中度
分點券商買賣超
```

AI 可以說：

```text
這檔股票近期價格轉強，同時投信連續買超，成交量也放大，可能代表中期資金開始關注。
```

AI 不可以在沒有資料時說：

```text
主力正在吃貨
外資大量布局
大戶偷偷買進
```

除非資料中確實有對應籌碼數據或可信新聞來源。

## 5.4 新聞面

新聞面用來理解公司、產業、市場與總經事件。

新聞必須包含：

```text
來源
日期
標題
URL
摘要
AI 解讀
不確定性說明
```

AI 不可以編造新聞，也不可以把沒有來源的市場傳聞當成事實。

## 5.5 跨市場連動

跨市場連動是本工具支援多市場的主要原因。

可分析：

```text
美股對隔日台股的可能影響
Nasdaq / S&P 500 / 費半對台股電子股的影響
美股大型科技股對台股 AI 供應鏈的影響
美債殖利率與美元對成長股的影響
BTC / ETH 對風險資產情緒的影響
VIX 對市場風險偏好的影響
ADR 對台股個股的參考價值
原油、黃金、原物料對相關產業的影響
```

跨市場連動不是必然因果。

系統可以說：

```text
可能影響
值得觀察
可能提高關注度
可能反映風險偏好變化
需要隔日市場驗證
```

不可說：

```text
一定會上漲
隔日必漲
必然受惠
一定會崩跌
```

---

# 6. 每日 8:30 市場晨報

## 6.1 晨報目標

每天早上 8:30 自動產出市場晨報，協助家庭成員快速理解：

```text
前一日美股發生什麼事
加密貨幣市場是否有重大波動
總經與風險情緒是否改變
這些變化可能如何影響當日台股
哪些台股族群或標的值得觀察
有哪些風險需要注意
```

## 6.2 晨報內容

每日晨報至少包含：

1. 今日簡短結論
2. 前一日美股摘要
3. 加密貨幣市場摘要
4. 主要指數與風險指標
5. 跨市場連動分析
6. 台股可能受影響族群
7. 技術面觀察
8. 籌碼面觀察
9. 重要新聞與事件
10. 法說會與公司公告重點，如資料可取得
11. 候選觀察標的
12. 今日風險提醒
13. 後續追蹤重點
14. 資料來源與資料日期
15. 給其他 AI 的分析包

## 6.3 Discord 晨報推送格式

Discord 只放簡短重點，不放完整長文。

範例：

```text
📊 每日市場晨報｜2026-06-01 08:30

簡短結論：
昨天美國科技股表現偏強，半導體類股同步上漲，加密貨幣市場也維持風險偏好。今天台股開盤時，電子股、半導體與 AI 供應鏈可能較受關注。不過仍需觀察台積電、AI 伺服器族群與大盤成交量是否同步支持。

重點：
- Nasdaq：+1.2%
- S&P 500：+0.8%
- 費城半導體指數 SOX：+1.5%
- 道瓊工業指數：+0.4%
- BTC 24h：+2.1%
- ETH 24h：+1.6%
- 風險情緒：偏 risk-on

台股觀察族群：
- 半導體
- AI 伺服器
- 電子權值股
- PCB / 散熱 / 電源

三大法人買超觀察：
- 2330 台積電
- 2317 鴻海
- 2382 廣達

三大法人賣超觀察：
- 2603 長榮
- 2888 新光金
- 2409 友達

重點觀察股票：
- 2330 台積電
- 2317 鴻海
- 2382 廣達
- 3231 緯創
- 6669 緯穎

完整報告：
https://report.yourdomain.com/reports/2026-06-01-morning

此結果僅供家庭內部市場研究使用，不構成投資建議。
```

---

# 7. Web Report Page

## 7.1 MVP 必備

Web Report Page 是 MVP 必備功能，不放到後續階段。

原因：

```text
Discord 不適合承載長篇研究報告
家庭成員用瀏覽器看完整內容更方便
報告需要可保存、可回看、可複製
底部需要提供可貼給其他 AI 的分析包
```

## 7.2 Web Report Page 功能

MVP Web Report Page 必須支援：

```text
每日晨報頁面
單股分析頁面
候選標的掃描報告頁面
歷史報告列表
Markdown 報告顯示
JSON 原始資料下載
複製給其他 AI 的分析包
簡易登入或密碼保護
```

## 7.3 URL 設計

使用 Cloudflare 網域與 Tunnel。

```text
https://report.yourdomain.com
```

頁面範例：

```text
/reports
/reports/2026-06-01-morning
/reports/2026-06-01-2330
/reports/2026-06-01-tw-scan
/api/reports/{report_id}.json
```

## 7.4 Report Page 結構

每份報告頁面包含：

```text
1. 報告標題
2. 產生時間
3. 報告類型
4. Data As Of
5. 使用資料來源
6. 簡短結論
7. 市場摘要
8. 技術面分析
9. 基本面分析
10. 籌碼面分析
11. 新聞與事件分析
12. 跨市場連動分析
13. 候選觀察標的
14. 風險與限制
15. 後續追蹤重點
16. 原始 JSON 下載
17. Markdown 下載
18. 給其他 AI 的分析包
19. 投資風險聲明
```

## 7.5 技術選型

MVP 建議直接放在 backend 裡，用 FastAPI server-side render。

建議：

```text
FastAPI
Jinja2 templates
Markdown rendering
Basic auth or simple login
Cloudflare Tunnel
```

後續若需要更漂亮，再改成：

```text
Next.js
React
Tailwind
```

## 7.6 Web Report Routes

```http
GET /
GET /reports
GET /reports/{report_id}
GET /reports/{report_id}/raw.json
GET /reports/{report_id}/download.md
GET /reports/{report_id}/copy-ai
```

---

# 8. 給其他 AI 的分析包

## 8.1 目的

Web Report Page 最底部必須有一個區塊，方便使用者直接複製到 Claude、ChatGPT、Gemini 或其他 AI 做進一步分析。

區塊名稱：

```text
給其他 AI 的分析包
```

或：

```text
Copy for Another AI
```

## 8.2 內容格式

此區塊應包含：

```text
使用者原始問題
報告產生時間
資料來源
資料日期
市場摘要
技術面重點
基本面重點
籌碼面重點
新聞重點與來源
跨市場連動推論
候選觀察標的
風險與限制
原始 JSON 摘要
請其他 AI 協助分析的指令
```

## 8.3 分析包模板

```markdown
# 給其他 AI 的分析包

請根據以下資料協助進一步分析市場狀況。

## 使用限制

請不要自行編造市場數據。
請不要引用沒有來源的新聞。
如果資料不足，請明確說明需要補哪些資料。
跨市場連動只能視為可能影響，不可視為必然因果。
此內容僅供家庭內部市場研究，不構成投資建議。

## 報告資訊

- 報告時間：{report_time}
- 報告類型：{report_type}
- 主要市場：美股、台股、加密貨幣
- 資料日期：{data_as_of}

## 市場摘要

{market_summary}

## 技術面

{technical_summary}

## 基本面

{fundamental_summary}

## 籌碼面

{chip_summary}

## 新聞與事件

{news_summary_with_sources}

## 跨市場連動

{intermarket_summary}

## 候選觀察標的

{watchlist}

## 風險與限制

{risk_notes}

## 請協助分析

1. 今天台股最需要注意哪些風險？
2. 哪些族群可能受到前一日美股或加密貨幣影響？
3. 候選觀察標的中，哪些只是短線題材，哪些可能有較完整支撐？
4. 哪些地方資料不足？
5. 還應該補哪些指標或新聞來源？
6. 請不要自行編造資料。
7. 請將事實、計算、推論與限制分開說明。
```

Web UI 應提供：

```text
Copy Markdown
Copy JSON
Download Markdown
Download JSON
```

---

# 9. 系統總體架構

## 9.1 高層架構

```text
External Data Sources
   ↓
Scheduler
   ↓
Market Data Processor
   ↓
Storage Manager
   ├─ Local Hot Storage，max 10GB
   ├─ Supabase Metadata
   └─ pCloud Cold Storage
   ↓
AI Market Research Assistant
   ↓
Verification Guardrail
   ↓
Research Pack Builder
   ├─ Markdown
   ├─ JSON
   └─ Copy for Another AI
   ↓
Web Report Page
   ↓
Discord Summary Push
```

## 9.2 使用者互動架構

```text
Family Member
   ├─ Discord
   │    ├─ 每日 8:30 簡短晨報
   │    ├─ 即時查詢
   │    └─ Web Report 連結
   │
   └─ Web Browser
        ├─ 完整報告
        ├─ 歷史報告
        ├─ Markdown / JSON 下載
        └─ Copy for Another AI
```

## 9.3 儲存架構

```text
Local Hot Storage，最多 10GB
   storage/local_parquet/
   storage/cache/
   storage/reports/
   storage/raw/
   storage/features/

Supabase Warm Metadata
   dataset_metadata
   report_index
   storage_objects
   usage_logs
   analysis_runs
   query_history
   cache_index

pCloud Cold Storage，2TB
   backups/parquet/
   backups/features/
   backups/reports/
   backups/analysis_json/
   backups/snapshots/
```

---

# 10. 本機資料上限 10GB

## 10.1 原則

本機資料儲存上限設定為 10GB。

本機只保留近期常用資料，pCloud 作為冷儲存。

原則：

```text
本機優先讀取
本機沒有才從 pCloud 下載
本機超過 10GB 自動清理
近期資料優先保留
歷史資料可從 pCloud 回補
Supabase 只存 metadata 與索引
```

## 10.2 儲存分層

```text
Local Hot Storage，最多 10GB
   最近常用市場資料
   最近 features
   最近報告
   cache
   當日與近期分析所需資料

pCloud Cold Storage，2TB
   長期歷史 parquet
   長期 feature dataset
   歷史 Markdown 報告
   歷史 JSON 分析包
   備份資料
```

## 10.3 本機資料保留策略

本機建議保留：

```text
最近 90 天價格資料
最近 90 天籌碼資料
最近 30 天新聞摘要
最近 30 天報告
常用 universe metadata
最近產生的 feature dataset
```

較舊資料：

```text
上傳到 pCloud
本機可刪除
需要時再下載
```

## 10.4 清理策略

當本機 storage 超過 10GB 時，系統依序清理：

1. 過期 cache
2. 已同步到 pCloud 的舊報告
3. 已同步到 pCloud 的舊 raw data
4. 已同步到 pCloud 的舊 feature dataset
5. 最久未使用的 parquet 檔案

不可清理：

```text
當日晨報資料
最近一次成功分析資料
尚未同步 pCloud 的報告
尚未同步 pCloud 的 JSON
metadata index
```

---

# 11. pCloud On-Demand Restore

## 11.1 觸發條件

當本機資料不足時，系統才從 pCloud 下載。

觸發條件：

```text
本機找不到需要的 parquet
本機資料日期不足
本機 row_count 不足
需要較長歷史區間計算指標
需要回看舊報告
需要補完整研究包
```

## 11.2 Restore Flow

```text
User Request / Scheduled Job
   ↓
Check Local Storage
   ↓
If local data exists and fresh enough:
   use local data
   ↓
If local data missing or insufficient:
   check Supabase dataset_metadata / storage_objects
   ↓
Find pCloud path
   ↓
Download required file only
   ↓
Verify checksum
   ↓
Load into local cache
   ↓
Continue analysis
   ↓
Check local 10GB limit
```

## 11.3 避免大量下載

pCloud restore 必須遵守：

```text
只下載需要的 symbol
只下載需要的 market
只下載需要的日期範圍
優先下載 feature dataset
必要時才下載 raw parquet
下載後重新檢查本機 10GB 上限
```

---

# 12. Storage Manager

## 12.1 職責

Storage Manager 負責：

```text
追蹤本機資料大小
檢查 10GB 上限
決定是否清理資料
決定是否從 pCloud restore
計算 checksum
同步資料到 pCloud
更新 Supabase metadata
更新 storage_objects
```

## 12.2 模組位置

```text
backend/storage/
  local_store.py
  pcloud_store.py
  supabase_store.py
  storage_manager.py
  eviction_policy.py
  restore_policy.py
```

## 12.3 Storage Manager API

```python
class StorageManager:
    def get_dataset(self, dataset_key, required_range):
        """
        先查 local。
        如果 local 不足，再查 Supabase metadata / storage_objects。
        如果 pCloud 有資料，下載需要的 dataset。
        最後回傳 local path。
        """

    def ensure_under_limit(self):
        """
        檢查 local storage 是否超過 10GB。
        如果超過，依 eviction policy 清理。
        """

    def sync_to_pcloud(self, local_path, remote_path):
        """
        將本機檔案同步到 pCloud。
        成功後更新 Supabase metadata / storage_objects。
        """

    def restore_from_pcloud(self, remote_path, local_path):
        """
        從 pCloud 下載檔案。
        下載後驗證 checksum。
        """
```

---

# 13. Scheduler 設計

## 13.1 每日排程

Scheduler 必須支援每日 8:30 自動產生晨報。

```text
每日 00:30
  更新加密貨幣歷史資料
  更新 BTC / ETH 與 Top Crypto 資料
  補抓缺漏資料
  同步至本機 parquet

每日 01:30
  更新台股價格資料
  更新台股籌碼資料
  更新法人買賣超
  更新融資融券資料
  建立台股 feature dataset

每日 03:00
  更新美股前一交易日資料
  更新主要指數資料
  更新 ADR 資料
  建立美股 feature dataset

每日 04:30
  更新總經與風險指標
  更新新聞摘要
  更新跨市場資料集

每日 05:30
  執行資料品質檢查
  驗證資料完整性
  補抓缺漏資料
  同步必要資料至 pCloud

每日 06:30
  產生 intermarket features
  產生台股觀察清單
  建立晨報分析 JSON

每日 08:00
  產生 Web Report
  產生 Markdown / JSON
  同步報告到 pCloud
  儲存 report_index / analysis_runs

每日 08:30
  推送 Discord 簡短摘要與 Web Report 連結
```

## 13.2 Catch-up 機制

如果 Windows 電腦在 8:30 未開機，系統無法準時推送。

因此 scheduler 啟動時必須做 catch-up：

```text
啟動時檢查今日晨報是否已產生
如果今日尚未產生，立即補產生
如果已產生但 Discord 未推送，補推送
如果資料不完整，標示資料限制
```

## 13.3 Morning Report Job

```python
def morning_report_job():
    update_required_market_data()
    build_intermarket_features()
    build_tw_watchlist()
    analysis_json = build_morning_analysis_json()
    ai_report = generate_ai_report(analysis_json)
    verified_report = run_guardrails(ai_report, analysis_json)
    report_paths = build_web_markdown_json_report(verified_report)
    sync_report_to_pcloud(report_paths)
    save_report_index(report_paths)
    send_discord_summary(report_paths.web_url)
```

---

# 14. Data Sources 設計

## 14.1 初期資料來源

| 類型       | 資料來源                           | MVP |
| ---------- | ---------------------------------- | --- |
| 美股價格   | yfinance                           | 是  |
| 台股價格   | yfinance / TWSE / TPEX             | 是  |
| 加密貨幣   | CoinGecko / Binance Public API     | 是  |
| 主要指數   | yfinance                           | 是  |
| 台股籌碼   | TWSE / TPEX / FinMind              | 是  |
| 新聞       | RSS / Yahoo Finance / 公開新聞來源 | 是  |
| 總經與風險 | yfinance / public data             | 是  |
| ADR        | yfinance                           | 是  |

## 14.2 Universe 初期設計

```text
US:
  Nasdaq 100
  主要科技股與半導體股
  加密貨幣相關美股

TW:
  台股成交值或市值前 200
  AI 供應鏈
  半導體
  電子權值股

Crypto:
  BTC
  ETH
  Top 50 crypto
```

## 14.3 資料取得原則

```text
先小型 universe
先日線資料
先 daily update
不要一開始做 tick data
不要一開始做全市場高頻掃描
資料先落地到 local parquet
Supabase 只存 metadata
pCloud 只做冷備份
```

---

# 15. Market Data Processor

## 15.1 職責

Market Data Processor 負責將原始資料轉成可分析 features。

它不做 AI 分析，只負責資料清洗與計算。

## 15.2 技術面 features

```json
{
  "symbol": "TSLA",
  "market": "US",
  "data_as_of": "2026-06-01",
  "technical": {
    "close": 238.5,
    "ma20": 225.1,
    "ma50": 210.7,
    "ma200": 180.2,
    "return_5d": 0.052,
    "return_20d": 0.184,
    "return_60d": 0.31,
    "volume_ratio_20d_60d": 1.72,
    "volatility_20d": 0.041,
    "trend_state": "uptrend"
  }
}
```

## 15.3 籌碼面 features

```json
{
  "symbol": "2330",
  "market": "TW",
  "data_as_of": "2026-06-01",
  "chip": {
    "foreign_net_buy_1d": 12000,
    "foreign_net_buy_5d": 35000,
    "investment_trust_net_buy_5d": 8000,
    "dealer_net_buy_5d": -1000,
    "institutional_net_buy_5d": 42000,
    "margin_balance_change_5d": 1500,
    "short_balance_change_5d": -200,
    "foreign_holding_ratio": 0.72
  }
}
```

## 15.4 跨市場 features

```json
{
  "data_as_of": "2026-06-01",
  "intermarket": {
    "nasdaq_return_1d": -0.021,
    "sp500_return_1d": -0.014,
    "sox_return_1d": -0.032,
    "vix_change_1d": 0.12,
    "usd_index_return_1d": 0.004,
    "us10y_yield_change": 0.06,
    "btc_return_24h": -0.078,
    "eth_return_24h": -0.065,
    "risk_sentiment": "risk_off"
  }
}
```

---

# 16. AI Market Research Assistant

## 16.1 職責

AI Market Research Assistant 負責根據系統整理好的資料做分析。

它可以：

```text
整理市場狀況
分析個股強弱
比較多檔股票
解釋技術面、基本面、籌碼面變化
摘要新聞與事件
分析跨市場連動
找出值得進一步研究的候選標的
提醒風險
提出後續觀察重點
產出研究報告
```

它不可以：

```text
捏造數據
捏造新聞
引用沒有來源的消息
把推論包裝成事實
給明確買賣指令
保證獲利
把跨市場影響說成必然因果
```

## 16.2 AI 分析輸出分層

AI 回答應區分：

```text
Fact: 來自資料源或新聞來源的事實
Calculation: 系統根據資料計算的結果
Inference: AI 根據事實與計算結果做出的推論
Limitation: 資料不足、限制與不確定性
```

## 16.3 不輸出 raw chain-of-thought

本工具不要求 AI 印出 raw chain-of-thought。

原因：

```text
chain-of-thought 不是資料來源
chain-of-thought 也可能合理化錯誤資料
```

本工具應輸出：

```text
Evidence Summary
Decision Rationale
Source-grounded Analysis
Fact / Calculation / Inference / Limitation
```

---

# 17. Gemini Prompt Template

```text
你是家庭內部使用的 AI 多市場研究助理。

你的任務是根據提供的市場資料、計算指標、籌碼資料、新聞來源與跨市場資料，協助使用者分析市場與候選標的。

你可以：
1. 分析股票、ETF、加密貨幣、產業與市場。
2. 根據資料提出候選原因與風險。
3. 比較不同標的的強弱。
4. 分析新聞可能影響。
5. 分析跨市場連動。
6. 提出後續觀察重點。
7. 協助產出可讀的研究報告。

你不可以：
1. 捏造價格、成交量、財報、籌碼、新聞或總經資料。
2. 引用沒有來源的新聞。
3. 產生 input JSON 中不存在的股票、數據或指標。
4. 把推論寫成事實。
5. 宣稱保證獲利。
6. 給出明確買賣指令。
7. 把跨市場關係說成必然因果。

回答規則：
1. 所有市場數據必須來自 input JSON。
2. 所有新聞必須附來源、日期與 URL。
3. 請將內容分成 Fact、Calculation、Inference、Limitation。
4. 如果資料不足，請明確說「資料不足，無法判斷」。
5. 如果是 AI 推論，必須標示為推論。
6. 回答最後必須包含：「此結果僅供家庭內部市場研究使用，不構成投資建議。」

以下是分析資料：
{analysis_result_json}
```

---

# 18. Verification Guardrail

## 18.1 目標

Guardrail 用來檢查 AI 是否超出資料範圍。

## 18.2 檢查項目

### Symbol Guard

AI 回答中不可出現 input JSON 以外的股票代號，除非該股票來自新聞來源或 cross-market context。

### Metric Guard

AI 不可引用 input JSON 中不存在的數值。

例如 JSON 沒有 PE，就不能說：

```text
本益比偏高
```

除非明確標示：

```text
目前缺少本益比資料，無法判斷估值是否偏高。
```

### News Citation Guard

只要 AI 提到新聞、報導、市場消息，就必須有：

```text
source
published_at
title
url
```

### Advice Guard

禁止語句：

```text
一定會漲
一定會跌
保證獲利
現在應該買進
現在應該賣出
滿倉
無風險
```

### Intermarket Causality Guard

跨市場連動不可寫成必然因果。

允許：

```text
可能影響
可能提高關注
值得觀察
需要隔日市場驗證
```

禁止：

```text
一定造成
必然導致
保證影響
隔日一定
```

### Data Age Guard

回答必須標示：

```text
data_as_of
資料來源
資料限制
```

---

# 19. Research Pack Builder

## 19.1 目標

Research Pack Builder 負責產出可保存、可複製、可給其他 AI 接續分析的研究包。

支援格式：

```text
Discord short summary
Web Report Page
Markdown report
JSON raw analysis pack
Copy for Another AI
```

## 19.2 Markdown Report 結構

```markdown
# AI 多市場研究報告

## 1. 使用者問題

{raw_query}

---

## 2. 簡短結論

{summary}

---

## 3. 使用資料

- 價格資料：
- 籌碼資料：
- 新聞資料：
- 跨市場資料：
- Data As Of：

---

## 4. 技術面觀察

{technical_analysis}

---

## 5. 基本面觀察

{fundamental_analysis}

---

## 6. 籌碼面觀察

{chip_analysis}

---

## 7. 新聞與事件觀察

{news_analysis}

---

## 8. 跨市場連動觀察

{intermarket_analysis}

---

## 9. AI 分析

{ai_analysis}

---

## 10. 風險與限制

{risk_notes}

---

## 11. 後續追蹤重點

{watch_items}

---

## 12. 給其他 AI 的分析包

{copy_for_another_ai}

---

## 13. 聲明

此結果僅供家庭內部市場研究使用，不構成投資建議。
```

---

# 20. API 設計

## 20.1 Analyze Endpoint

```http
POST /analyze
```

Request:

```json
{
  "user_id": "discord_user_id",
  "query": "分析 2330 最近強不強",
  "options": {
    "need_export_pack": true,
    "market": "TW"
  }
}
```

Response:

```json
{
  "summary": "2330 目前技術面偏強，但仍需觀察籌碼是否延續。",
  "analysis_result": {},
  "files": {
    "markdown": "storage/reports/report_20260601_2330.md",
    "json": "storage/reports/report_20260601_2330.json"
  },
  "web_url": "https://report.yourdomain.com/reports/2026-06-01-2330"
}
```

## 20.2 Scan Endpoint

```http
POST /scan
```

Request:

```json
{
  "user_id": "discord_user_id",
  "market": "TW",
  "scan_type": "technical_chip",
  "universe": "tw_top200",
  "need_export_pack": true
}
```

## 20.3 Morning Brief Endpoint

```http
POST /brief/morning
```

用途：

```text
整理前一日美股、加密貨幣、總經與隔日台股可能風險。
```

## 20.4 Report Endpoint

```http
GET /reports/{report_id}
GET /reports/{report_id}/raw.json
GET /reports/{report_id}/download.md
GET /reports/{report_id}/copy-ai
```

---

# 21. Discord Bot 設計

## 21.1 支援指令

```text
@bot 分析 2330
@bot 分析 TSLA
@bot 分析 BTC
@bot 找最近比較強的台股
@bot 找技術面轉強、籌碼也改善的股票
@bot 今天美股會怎麼影響明天台股？
@bot BTC 大跌會不會影響美股？
@bot 產出今天市場研究包
@bot 今日晨報
```

## 21.2 Discord 回覆格式

```text
📊 AI 多市場研究助理

簡短結論：
2330 目前技術面偏強，但仍需要觀察籌碼與大盤是否同步支持。

使用資料：
- 價格：yfinance / TWSE
- 籌碼：TWSE / FinMind
- 新聞：公開新聞來源
- 資料日期：2026-06-01

主要觀察：
- 技術面：
- 籌碼面：
- 新聞面：
- 跨市場：

完整報告：
https://report.yourdomain.com/reports/2026-06-01-2330

風險與限制：
- 本次分析尚未納入完整財報資料
- 跨市場影響不是必然因果
- 新聞影響可能已反映在價格

此結果僅供家庭內部市場研究使用，不構成投資建議。
```

💰 本次 AI 成本估算：NT$0.5
📊 今日累計：NT$12 / NT$30

## 21.3 Anti-spam

```text
每位使用者 cooldown 5 秒
每日 Gemini 成本限制
cache 命中優先回覆
長任務避免重複觸發
管理員可解除限制
```

---

# 22. Supabase Schema

## 22.1 dataset_metadata

```sql
CREATE TABLE dataset_metadata (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol text NOT NULL,
    market text NOT NULL,
    data_source text NOT NULL,
    data_type text,
    start_date date,
    end_date date,
    last_updated_at timestamptz,
    dataset_version text,
    feature_version text,
    row_count integer,
    checksum text,
    local_path text,
    pcloud_path text,
    created_at timestamptz DEFAULT now()
);
```

## 22.2 report_index

```sql
CREATE TABLE report_index (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_type text NOT NULL,
    title text NOT NULL,
    report_date date NOT NULL,
    data_as_of date,
    market_scope text[],
    local_markdown_path text,
    local_json_path text,
    pcloud_markdown_path text,
    pcloud_json_path text,
    public_url text,
    discord_pushed boolean DEFAULT false,
    created_at timestamptz DEFAULT now()
);
```

## 22.3 storage_objects

```sql
CREATE TABLE storage_objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_key text UNIQUE NOT NULL,
    object_type text,
    market text,
    symbol text,
    start_date date,
    end_date date,
    local_path text,
    pcloud_path text,
    size_bytes bigint,
    checksum text,
    last_accessed_at timestamptz,
    synced_to_pcloud boolean DEFAULT false,
    created_at timestamptz DEFAULT now()
);
```

## 22.4 usage_logs

```sql
CREATE TABLE usage_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text,
    query text,
    intent text,
    model text,
    input_tokens integer,
    output_tokens integer,
    estimated_cost_twd numeric,
    created_at timestamptz DEFAULT now()
);
```

## 22.5 analysis_runs

```sql
CREATE TABLE analysis_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text,
    raw_query text,
    intent text,
    market text,
    symbols text[],
    data_as_of date,
    summary text,
    markdown_path text,
    json_path text,
    web_url text,
    pcloud_markdown_path text,
    pcloud_json_path text,
    created_at timestamptz DEFAULT now()
);
```

## 22.6 query_history

```sql
CREATE TABLE query_history (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text,
    query text,
    normalized_query text,
    response_summary text,
    report_id uuid,
    created_at timestamptz DEFAULT now()
);
```

## 22.7 cache_index

```sql
CREATE TABLE cache_index (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cache_key text UNIQUE NOT NULL,
    cache_type text,
    local_path text,
    pcloud_path text,
    expires_at timestamptz,
    created_at timestamptz DEFAULT now()
);
```

---

# 23. pCloud Storage 設計

## 23.1 pCloud 目錄

```text
/AI-Market-Research/
  /backups/
    /parquet/
      /US/
      /TW/
      /CRYPTO/
    /features/
    /reports/
      /markdown/
      /json/
    /snapshots/
  /logs/
```

## 23.2 pCloud 使用原則

```text
本機先完成運算
本機產生 parquet / report
排程同步到 pCloud
Supabase 存 pCloud path 與 checksum
不要直接從 pCloud 做大量分析
不要把 pCloud 當資料庫
```

## 23.3 同步策略

```text
每日資料品質檢查後同步必要 parquet / feature
每次產生 report 後同步 Markdown / JSON
每週做完整 metadata 檢查
失敗時保留本機檔案，下一次重試
```

---

# 24. Cache 設計

## 24.1 Cache Key

```text
{intent}:{market}:{universe}:{data_as_of}:{feature_version}
```

例如：

```text
scan:TW:tw_top200:2026-06-01:feature_v1
```

## 24.2 Cache Policy

| 類型              | TTL                             |
| ----------------- | ------------------------------- |
| Router result     | 1 day                           |
| Price features    | until data_as_of changes        |
| Chip features     | until data_as_of changes        |
| Intermarket brief | 12 hours                        |
| Gemini summary    | until input JSON changes        |
| Report            | local 30 days, pCloud long-term |

---

# 25. Cost Control

## 25.1 原則

```text
資料計算不用 Gemini
只有摘要、分析、報告生成才用 Gemini
先產生 structured JSON，再丟給 Gemini
cache 命中不重複呼叫 Gemini
每日成本上限
每位使用者查詢上限
```

## 25.2 預算檢查

```python
def check_daily_budget(user_id):
    spent = get_today_spent(user_id)
    limit = get_daily_limit(user_id)

    if spent >= limit:
        return False, spent, limit

    return True, spent, limit
```

## 25.3 Discord 成本回覆

```text
💰 本次 AI 成本估算：NT$0.5
📊 今日累計：NT$12 / NT$30
```

---

# 26. Docker 架構

## 26.1 docker-compose

```yaml
services:
  backend:
    build: ./backend
    container_name: ai-market-backend
    env_file:
      - .env
    volumes:
      - ./storage:/app/storage
      - ./configs:/app/configs
    ports:
      - "8000:8000"
    depends_on:
      - redis

  discord-bot:
    build: ./bot
    container_name: ai-market-discord-bot
    env_file:
      - .env
    depends_on:
      - backend

  scheduler:
    build: ./scheduler
    container_name: ai-market-scheduler
    env_file:
      - .env
    volumes:
      - ./storage:/app/storage
      - ./configs:/app/configs
    depends_on:
      - backend
      - redis

  redis:
    image: redis:7
    container_name: ai-market-redis
    volumes:
      - redis_data:/data

  cloudflared:
    image: cloudflare/cloudflared:latest
    container_name: ai-market-cloudflared
    command: tunnel --no-autoupdate run
    env_file:
      - .env
    depends_on:
      - backend

volumes:
  redis_data:
```

---

# 27. .env 設計

```text
# Discord
DISCORD_TOKEN=
DISCORD_GUILD_ID=
DISCORD_CHANNEL_ID=

# Gemini
GEMINI_API_KEY=
GEMINI_MODEL=

# Supabase
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=

# pCloud
PCLOUD_ACCESS_TOKEN=
PCLOUD_ROOT_FOLDER=/AI-Market-Research

# Cloudflare
CLOUDFLARE_TUNNEL_TOKEN=
PUBLIC_REPORT_BASE_URL=https://report.yourdomain.com

# Web
WEB_REPORT_USERNAME=
WEB_REPORT_PASSWORD=
WEB_REPORT_SECRET_KEY=

# Storage
LOCAL_STORAGE_LIMIT_GB=10
LOCAL_STORAGE_PATH=/app/storage

# Scheduler
TIMEZONE=Asia/Taipei
MORNING_REPORT_TIME=08:30
```

---

# 28. 建議資料夾結構

```text
ai-market-research-assistant/
  bot/
    Dockerfile
    discord_bot.py
    commands/
      analyze.py
      scan.py
      morning_brief.py

  backend/
    Dockerfile
    main.py
    api/
      analyze.py
      reports.py
      health.py
    router/
      intent_router.py
      request_validator.py
    data_sources/
      yfinance_loader.py
      twse_loader.py
      tpex_loader.py
      finmind_loader.py
      coingecko_loader.py
      binance_loader.py
      news_loader.py
    processor/
      price_features.py
      fundamental_features.py
      chip_features.py
      crypto_features.py
      intermarket_features.py
    ai/
      gemini_client.py
      prompts.py
      schemas.py
    guardrails/
      symbol_guard.py
      metric_guard.py
      news_citation_guard.py
      advice_guard.py
      intermarket_guard.py
    reports/
      markdown_builder.py
      json_builder.py
      web_renderer.py
      copy_for_ai_builder.py
    storage/
      local_store.py
      pcloud_store.py
      supabase_store.py
      storage_manager.py
      eviction_policy.py
      restore_policy.py
    cost/
      usage_tracker.py
      budget_guard.py
    templates/
      base.html
      report.html
      reports_index.html

  scheduler/
    Dockerfile
    scheduler.py
    jobs/
      update_us_prices.py
      update_tw_prices.py
      update_crypto_prices.py
      update_news.py
      update_macro.py
      build_morning_report.py
      sync_pcloud.py

  configs/
    universe/
      us_nasdaq100.yaml
      tw_top200.yaml
      crypto_top50.yaml
    features/
      technical_v1.yaml
      chip_v1.yaml
      intermarket_v1.yaml

  storage/
    local_parquet/
    cache/
    reports/
    raw/
    features/
    logs/

  docker-compose.yml
  .env.example
  README.md
```

---

# 29. Security / Access Control

## 29.1 Secrets

所有 secrets 放 `.env`，不可 commit。

```text
DISCORD_TOKEN
GEMINI_API_KEY
SUPABASE_SERVICE_ROLE_KEY
PCLOUD_ACCESS_TOKEN
CLOUDFLARE_TUNNEL_TOKEN
WEB_REPORT_PASSWORD
```

## 29.2 Discord 權限

```text
只允許指定 server
只允許指定 channel
只允許家庭成員 user id
管理員可設定成本限制
```

## 29.3 Web Report 權限

MVP 至少使用：

```text
簡易登入
Basic auth
固定密碼
```

後續升級：

```text
Cloudflare Access
家庭成員 email allowlist
```

## 29.4 投資風險聲明

每次分析都要包含：

```text
此結果僅供內部市場研究使用，不構成投資建議。
```

---

# 30. Error Handling

## 30.1 資料不足

```text
資料不足，無法分析 {symbol}。
原因：最近 60 日價格資料不足。
```

## 30.2 新聞無來源

```text
目前沒有可引用新聞來源，因此本次不納入新聞面分析。
```

## 30.3 Gemini 失敗

```text
AI 分析生成失敗，但系統已保留原始資料與計算結果。
```

## 30.4 Guardrail 失敗

```text
AI 回答可能超出資料範圍，系統已阻擋。
以下提供原始資料摘要與可驗證指標。
```

## 30.5 pCloud 同步失敗

```text
報告已保存在本機，但 pCloud 備份失敗，將於下次排程重試。
```

## 30.6 Web Report 產生失敗

```text
Discord 仍可推送簡短摘要，但完整 Web Report 產生失敗。
系統會保留 analysis JSON 並於下次排程重試。
```

---

# 31. Phase Plan

## Phase 1：MVP 必備功能

目標：完成每日自動晨報、Discord 摘要、Web Report Page、10GB local-first storage 與基本 AI 分析流程。

必備功能：

```text
Docker Compose
Backend API
Discord Bot
Scheduler
Web Report Page
Cloudflare Tunnel
每日 8:30 晨報
Gemini 分析
Markdown / JSON 研究包
Web Report 底部貼給其他 AI 的分析包
Supabase report_index / usage_logs / storage_objects
pCloud 報告備份
本機資料 10GB 上限
Storage Manager
Local-first, pCloud on-demand restore
基本技術面 features
基本跨市場 features
基本新聞來源引用
基本 Guardrail
```

成功標準：

```text
每天 08:30 Discord 自動收到市場晨報摘要
摘要包含完整 Web Report 連結
家庭成員可以用瀏覽器看完整報告
Web Report 可以下載 Markdown / JSON
Web Report 底部可以複製給其他 AI 的分析包
本機資料超過 10GB 會自動清理
本機資料不足時可從 pCloud restore
```

## Phase 1.5：台股籌碼與資料完整度強化

功能：

```text
TWSE / TPEX / FinMind 籌碼資料
三大法人買賣超
融資融券
台股籌碼面候選清單
法說會與公司公告資料整理
ADR 資料
更穩定的新聞來源處理
資料品質檢查強化
```

成功標準：

```text
@bot 找技術面轉強、籌碼也改善的股票
```

可以回覆：

```text
技術面候選
籌碼面觀察
法人買賣超
風險與限制
Web Report 連結
```

## Phase 2：Web Report 體驗與基本面強化

功能：

```text
更完整基本面
更漂亮 Web UI
歷史查詢
報告比較
PDF export
Cloudflare Access 強化
報告搜尋
家庭成員偏好設定
```

## Phase 3：進階研究與回測

功能：

```text
多因子候選
產業比較
候選標的後續表現追蹤
vectorbt 或其他 backtesting engine
策略回測
Strategy Performance Report
```

注意：

```text
即使加入回測，也不代表自動交易。
```

---

# 32. MVP 實作順序

建議照這個順序做：

```text
1. 建 repo 與 docker-compose
2. 建 backend FastAPI
3. 建 Web Report 基本頁面
4. 建 Cloudflare Tunnel
5. 建 Discord Bot
6. 實作 /brief/morning endpoint
7. 實作 scheduler 與每日 08:30 job
8. 實作 yfinance loader
9. 實作 crypto loader
10. 實作 technical features
11. 實作 intermarket features
12. 實作 analysis JSON builder
13. 實作 Gemini summary
14. 實作 basic guardrail
15. 實作 Markdown / JSON report builder
16. 實作 Copy for Another AI builder
17. 實作 Supabase report_index / usage_logs
18. 實作 pCloud report backup
19. 實作 Storage Manager 10GB limit
20. 實作 pCloud on-demand restore
21. 加入 Discord 晨報推送
22. 加入即時查詢 analyze / scan
23. 加入台股籌碼資料
```

---

# 33. 最終結論

本系統是一個：

```text
家庭用
Discord 提醒
Web Report 閱讀
本機 Docker 運行
每日 8:30 自動晨報
資料可追蹤
新聞可引用
AI 可分析
不自動交易
不保證獲利
可產出研究包
可觀察跨市場連動
本機資料上限 10GB
pCloud 作為冷儲存
```

的 AI 多市場研究助理。

現有資源的最佳分工是：

```text
Windows 本機 + Docker：
  主要運算與服務執行

Discord：
  晨報摘要、即時查詢、Web Report 連結

Web Report Page：
  完整報告、歷史報告、Markdown / JSON、Copy for Another AI

Gemini API：
  AI 分析與報告生成

Supabase Free Tier：
  metadata、usage logs、report index、storage index

pCloud API 2TB：
  parquet、feature、report、JSON 的冷儲存與備份

Cloudflare 網域 + Tunnel：
  安全提供 Web Report Page
```

最重要的設計原則是：

```text
AI 可以幫忙分析，但不能捏造資料。
新聞必須有來源。
數據必須來自資料源或系統計算。
跨市場連動只能說可能影響，不能說必然發生。
候選標的是研究方向，不是買進建議。
Discord 只放重點，完整內容放 Web Report。
Web Report 最底部提供給其他 AI 的分析包。
本機資料最多 10GB，不足時才從 pCloud 按需下載。
```
