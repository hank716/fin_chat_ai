# fin_chat_ai 優化計畫(交由執行模型分批實作)

> 本文件是頂層規劃產物。執行者(如 Opus)請嚴格照 WP 順序與驗收條件執行,
> **不得自行做架構判斷**——所有架構決策已在「全域決策」定案。
> 一次只做一個 WP,完成驗收後才進下一個。

## Context

fin_chat_ai 是台股為主的晨報選股系統:規則式選股 + sklearn HistGradientBoosting(edge/risk/rank/meta 四類模型)+ qlib_offline 隔離容器(Alpha158+LightGBM),已有 purged walk-forward + embargo、triple-barrier、事後回測閉環等嚴謹設計。本計畫目標:**提升預測準確度**(首要)+ 清工程債(次要)。

探查確認的核心問題(按影響排序):

1. **無除權息還原股價**——TWSE/FinMind 都用原始 close(FinMind 有 `TaiwanStockPriceAdj` 未用);除息跳空污染所有 return/波動/MA 特徵、triple-barrier 標籤、回測 P&L。單一最大準確度污染源。
2. **成績數字全是註解、非實測**——`storage/strategy/` 為空,模型在此 checkout 從未實跑;rank-IC≈0.027、risk AUC≈0.69、小型股 IC≈0.06 皆待驗證。無 baseline 就無法歸因任何改善。
3. **全 repo 零測試**——無 tests/ 目錄;各 spec tasks.md 的「測試基線 Phase」全部未勾選(42+ 項)。
4. 資料缺口:TWII 指數 parquet 只有近月(`vs_index` 特徵舊日期全 NaN)、TAIFEX P/C 無 known_date(訓練端 D 日特徵用 D 日盤後 P/C = 洩漏半天)、survivorship bias 未量化。
5. 實質 bug:`gemini_client._usage_of` 誤掛 @retry 而 `_generate_json` 反而沒有 retry;`fundamentals._pick` 子字串 fallback 可能取錯財報科目;`ask.py` Q&A 完全沒接 guardrail;narrative 敘事數字不被驗證。
6. serving 重排順序 edge→risk→rank→qlib→meta 是 last-writer-wins,歸因不明;方向模型撞效率牆但風險模型/小型股 sleeve 有 edge,火力應重分配。

## 全域決策(executor 不得偏離)

- **D1 歸因方法論**:任何準確度改動,必須在同一份資料快照(固定 `max_date`)上改動前後各跑一次評估對照。禁止跨日對照(parquet 每日增量會污染歸因)。
- **D2 除權息策略**:採「事件因子表 + 讀取端還原」,不重寫既有 parquet、不並存 adj_close 欄位。使用邊界:**報酬/波動/MA/標籤/movers 排行用還原價;觸價判定(target_hit/stop_hit)與所有對使用者顯示的價格用原始價**。
- **D3 Spec Kit 分類**:改 serving 行為或新增資料資產 → `/speckit-specify` 新規格;bug fix、補測試、執行既有 spec 遺留 task → 直接改或 `/speckit-converge`。每個 WP 已標注。
- **D4 FEATURE_COLUMNS 紀律**:`backend/reports/backtest.py:157` 的 `FEATURE_COLUMNS` 是 train/serve 共用 SSOT;增刪欄位必須同一 PR 同步訓練端(`training_set._symbol_long`)與 serving 端(`processor/tw_features.py`),必附兩端 parity 測試,並重建 training_set 重訓四類模型。
- **通用紀律**:每 WP 單獨分支/提交,commit 遵循 Conventional Commits 並引用對應 `specs/NNN`;準確度 WP 的 PR/commit 必附 `eval_snapshot --compare` 輸出;**禁止在同一 WP 內順手重構未列出的檔案**。

---

## Phase 0 — 評估基線與安全網(全部完成後才能動 Phase 1)

### WP0.1 pytest 基礎建設 + 防洩漏測試套件
- **類型**:直接改(執行 specs/013、014 既列測試任務)|**規模**:M|**依賴**:無
- **涉及檔案**:新增 `tests/conftest.py`、`tests/test_training_set_leakage.py`、`tests/test_backtest_leakage.py`、pytest 設定(`pythonpath = backend`)
- **要點**:全部用合成價格 fixture,不依賴真實 parquet;monkeypatch `storage.local_store.read_prices/read_chip/read_margin`。覆蓋 7 組:
  1. `_triple_barrier`(training_set.py:46):只掃 [t+1, t+h]、同日雙觸取下界、窗未滿 NaN
  2. `fwd_return/fwd_mae/fwd_vol` 標籤:對 t 注入未來一根極端 bar → t 日**特徵**不變、僅標籤變(reversed-rolling `shift(-1)` 語意)
  3. 基本面 `merge_asof` point-in-time(training_set.py:232):`known_date` 在 trade_date 之後的財報不得出現在特徵
  4. `backtest._forward_window`(backtest.py:95):嚴格 `trade_date > as_of`
  5. `_overlap_weights`(training_set.py:363):同檔同窗重疊樣本權重 = 1/重疊數
  6. purged walk-forward 切分:embargo 內樣本不得進訓練折
  7. `featurize`(backtest.py:200):train/serve 欄位 parity(平面欄位 vs 巢狀 fundamentals fallback)
- **驗收**:`pytest -q` 全綠;每組附至少 1 個「故意打破會 fail」的證明(例:把 `shift(-h)` 改 `shift(h)` 測試確實紅掉)

### WP0.2 Baseline 實跑 + eval snapshot 機制
- **類型**:直接改|**規模**:M|**依賴**:WP0.1
- **涉及檔案**:新增 `backend/reports/eval_snapshot.py`;微改 `training_set._build_big` 加可選 `max_date` 截止參數
- **要點**:
  1. CLI `python -m reports.eval_snapshot --tag baseline`:依序 `build_training_set()` → `train_edge_model()/train_risk_model()/train_rank_model()/train_meta_model()` → `evaluate_effectiveness()`
  2. 快照 JSON 落 `storage/strategy/eval_history/{YYYYMMDD}_{tag}.json`:git SHA、資料截止日、樣本數、各 h 的 edge AUC / risk AUC / rank-IC / ICIR / precision@k / meta AUC、小型股 sleeve 指標、當時 config gates
  3. `--compare A B` 子命令輸出逐指標 delta 表
- **驗收**:`storage/strategy/` 出現 training_set.parquet、各 h 模型檔、baseline 快照;rank-IC/AUC 為非 null 實數;附「註解宣稱值(0.027/0.69/0.06)vs 實測值」對照表

### WP0.3 TAIFEX P/C known_date 與盤前取值正確性
- **類型**:`/speckit-converge` specs/016(執行遺留 T014)|**規模**:S|**依賴**:WP0.2
- **涉及檔案**:`backend/data_sources/taifex_loader.py`(寫入/讀取加 `known_date` 欄;TAIFEX 盤後 ~15:00 公布 → known_date = trade_date 收盤後)、`backend/processor/market_regime.py`(`latest_pc_features` 過濾 `known_date <= now` 後取尾)
- **要點**:訓練端 `_build_big`(training_set.py:424-429)目前以 trade_date 直接 merge,D 日特徵用 D 日盤後 P/C = 洩漏半天 → 改 map 到 D+1 交易日(或 merge_asof backward on known_date),**train/serve 同步改**
- **驗收**:測試——偽造「D+1 早上 07:30、parquet 已有 D 日 P/C」→ 取 D 日值;訓練端 D 日樣本的 `pc_oi_ratio` 等於 D-1 日值;跑快照對照(pc 特徵分布會輕微變動)

### WP0.4 fundamentals._pick 子字串 fallback 修正
- **類型**:直接改(bug fix)|**規模**:S|**依賴**:WP0.1
- **涉及檔案**:`backend/processor/fundamentals.py:221-235`、`backend/processor/fundamentals_history.py`(同款邏輯同步)
- **要點**:移除「子字串包含」fallback,改:精確比對 → case-insensitive 精確比對 → 顯式別名對照表 `_TYPE_ALIASES: dict[str, tuple[str, ...]]`(枚舉 FinMind 已知版本差異);未匹配時 `logger.warning` 一次(帶 symbol + candidates + 實際 keys 樣本)
- **驗收**:構造含 `Revenue` 與 `TotalNonoperatingIncomeAndExpense` 等干擾科目的測試,斷言絕不誤配;抽查 2330 `gross_margin_pct` 在 50-60% 合理區間

---

## Phase 1 — 資料正確性(最大準確度污染源)

### WP1.1 除權息事件因子表
- **類型**:**`/speckit-specify` 新規格 `017-adjusted-prices`**|**規模**:M|**依賴**:Phase 0 完成
- **涉及檔案**:`backend/data_sources/finmind_loader.py`(已有 `get_dividend`;補 `TaiwanStockDividendResult` 端點取 before_price/after_price)、新增 `backend/processor/adj_factors.py`(build/refresh/read)、`backend/storage/local_store.py`(`read_adj_factors()`)
- **要點**:
  1. 建 `storage/local_parquet/tw_adj_factors.parquet`(schema: `symbol, ex_date, adj_factor, source`);`adj_factor = after_price_reference / before_price`(fallback:用 `get_dividend` 現金股利+配股自算參考價)
  2. 只為 parquet 目錄既有 ~2366 檔建表,走 rate_limiter 分批一次性回補(FinMind 免費 600 req/hr,需支援斷點續跑)
  3. 增量維護:掛進 `morning_brief._run_backtest_loop()` guarded 區,每日只查近 7 日除權息
  4. sanity:factor 必須 ∈ (0.5, 1.0],否則丟棄並 log
- **驗收**:因子表列數 > 0;抽查 3 個已知案例(2330 每季除息、0056 高配息、一檔有配股個股)factor 手算誤差 <0.1%;公式與 sanity 單元測試

### WP1.2 讀取端還原 + 訓練/回測/serving 三處切換 + 重訓對照
- **類型**:同 spec 017 第二批 tasks|**規模**:L|**依賴**:WP1.1、WP1.3
- **涉及檔案與要點**:
  1. `local_store.read_prices` 加 `adjusted: bool = False`:讀 factors 後對 open/high/low/close 做 backward 累積還原(ex_date 前所有價 ×∏factor)
  2. `training_set._symbol_long`、`_index_trailing` 改 `adjusted=True`(triple-barrier 與 fwd_mae/vol/absmove 標籤自動繼承)
  3. `backtest.py` 雙軌:`forward_return_pct/mfe/mae/vs_index` 用還原價,`target_hit/stop_hit` 觸價判定維持原始價;scorecard 加 `price_basis: "adjusted"` 世代標注,舊 scorecard 不重算
  4. `tw_features.py` serving 端 return/dist_ma/volatility/movers 改還原價;**顯示欄位 close、目標價、止損價維持原始價**
  5. 訓練與 serving **同一 PR 切換**(D4);切換後 `eval_snapshot --tag adj_prices` 與 baseline 用同一 `max_date` 對照
- **驗收**:(a) 洩漏測試全綠;(b) 整合測試:已知除息日 `return_1d_pct` 不再假跳空;(c) 快照對照報告落地(不設「必須變好」門檻——這是正確性修復);(d) 晨報實跑,tw_watchlist 顯示名目價

### WP1.3 TWII 指數歷史回補
- **類型**:直接改|**規模**:S|**依賴**:Phase 0;可與 WP1.1 並行,**須在 WP1.2 重訓前完成**
- **涉及檔案**:`backend/data_sources/backfill_tw.py` 或 `history_crawl.py`(加 TWII 回補:FinMind TAIEX 或 yfinance `^TWII`,對齊 PRICE_COLUMNS 落 `local_parquet/tw/TWII.parquet`,upsert 去重)
- **驗收**:`read_prices("TWII","tw")` ≥480 列;重建 training_set 後 `vs_index_20d_pct` 非空率 >90%;更新 training_set.py:94-96 過時註解;快照對照

### WP1.4 Survivorship bias 稽核
- **類型**:先稽核腳本;缺口大再開 spec `018-pit-universe`|**規模**:S(稽核)|**依賴**:Phase 0
- **要點**:已知事實——全市場回補走 TWSE/TPEX 逐日全市場端點,回補窗內下市股其實有資料在 parquet;`_build_big` 掃 parquet glob 而非 universe。偏差主要在:(a) 回補起始日前已下市者缺席;(b) 下市股 `sector_of()` 回 None → sector 特徵 NaN;(c) ETF 過濾對 sector=None 失效。稽核 = parquet 股票 vs universe 差集清單、差集樣本占比、其 fwd_return 分布 vs 全體
- **驗收**:稽核數字落地;占比 <2% → 記錄為已知限制關閉;≥2% → 開 spec 018(universe 每日快照 `tw_all_{date}.json` + sector fallback)

---

## Phase 2 — 模型效能重分配(每項獨立 A/B)

### WP2.1 Phase 1 後基線重定 + qlib_offline 實跑驗證
- **類型**:直接改(執行 specs/015 流程)|**規模**:M|**依賴**:Phase 1 完成
- **要點**:qlib 容器內 `dump → run`(`dump.py` 需改吃還原價等價邏輯);回答懸而未決的「Alpha158+LightGBM 能否破 0.03 方向牆」;serving 端 `_latest_qlib_scores` 自動吃 gate 判定
- **驗收**:qlib eval JSON `rank_ic` 非 null;結論(破/不破)寫進 eval_history 快照;不破 → config 註明 qlib 重排長期停用依據

### WP2.2 Serving 融合順序重構:last-writer-wins → 明確優先序
- **類型**:**`/speckit-specify` 新規格 `019-score-fusion`**|**規模**:M|**依賴**:WP2.1
- **涉及檔案**:`morning_brief.py:352-388`(五個 `_apply_*` 收斂成 `_apply_all_scores` 單一入口)、`strategy_calibration.py`(新增 `fuse_scores(candidates) -> dict[sym, {score, components}]`)
- **要點**:規則——通過各自 gate 的分數 z-score 標準化後加權平均,權重 = 各模型 OOS 指標超出 gate 的幅度;全部未過 gate → 不重排。report JSON 保留個別 `edge_scores/risk_scores/...` 欄位(向後相容),新增 `fused_scores` 與 `fusion_weights`;重排只發生一次
- **驗收**:測試——僅 risk 過 gate → 排序 = risk 排序;多模型過 gate → 加權結果;全不過 → 順序不動;晨報實跑 report JSON 含 fusion_weights

### WP2.3 中小型股 sleeve 正式化
- **類型**:併入 spec 019|**規模**:M|**依賴**:WP2.2(且 WP0.2 實測確認小型股 IC 屬實)
- **涉及檔案**:`training_set.py`(band 參數已支援,補常態排程)、`strategy_calibration.py`(`_rank_model_path` 加 band 維度、`score_rank` 依候選 amount 選模型)、`morning_brief.py`(candidates 帶 amount)
- **驗收**:band 模型檔與 meta JSON 落地、OOS rank-IC 實測記錄;serving 測試:amount<50M 候選走 band 模型;快照對照

### WP2.4 新特徵 A/B(子項獨立提交、獨立快照)
- **類型**:**`/speckit-specify` 新規格 `020-feature-batch`**|**規模**:各 M|**依賴**:WP2.1;子項間無依賴
- **子項**(優先序):
  1. **股利/除息事件特徵**(WP1.1 副產品,幾乎零成本):`days_to_ex_dividend`、`dividend_yield_pct` 進 FEATURE_COLUMNS;訓練端由 factor 表 merge_asof、serving 端 tw_features 注入
  2. **新聞情緒量化**:每日每股 `news_count_5d`、`news_sentiment_5d`(先詞典法零成本;LLM 批次打分列後續選項附成本估算);新聞 `date` 當 known_date
  3. **TAIFEX P/C 細項 + VIX**(spec 016 T015 遺留):可取得則接入 `market_regime` 同款路徑
- **驗收**(每子項):兩端 parity 測試;快照對照表;**回退條件明定:主指標 delta ≤0 且 ICIR 未升即回退該欄位**

---

## Phase 3 — LLM 層與工程債(可與 Phase 1/2 交錯)

### WP3.1 gemini_client retry 修正
- **類型**:直接改(bug fix)|**規模**:S|**依賴**:無,可隨時做
- **要點**:(a) 移除 `_usage_of`(gemini_client.py:67-72)誤掛的 @retry(純函式);(b) `_generate_json`(:102)補單層 @retry(retry on `httpx.RequestError, GeminiUnavailable`, stop=4);(c) `generate_text`(:201-212)移除疊層(現況兩層 = 最多 3×4=12 次呼叫,留一層 stop=4)
- **驗收**:mock httpx——503×3 後 200 → `_generate_json` 成功且共 4 次呼叫;429 → 1 次即拋 QuotaExceeded;generate_text 對 503 恰好重試至 4 次上限

### WP3.2 ask.py Q&A 接 guardrail
- **類型**:`/speckit-converge` specs/004、008|**規模**:M|**依賴**:WP3.1(同檔案群避免衝突)
- **要點**:`backend/api/ask.py` 目前零驗證。復用 `run_guardrails` 的 symbol/禁語子集做「輕量模式」(Q&A 無 features 上下文):提及代號必須存在於 universe、禁語掃描
- **驗收**:回答含不存在代號「9999」→ 被標注/清理;含禁語 → 被攔;正常回答不變;`/ask` 端點整合測試

### WP3.3 narrative 數字驗證(guardrail 強化)
- **類型**:**`/speckit-specify` 新規格 `021-narrative-number-guard`**|**規模**:M|**依賴**:WP3.2
- **要點**:目前 guardrail 只驗 `evidence[].source_ref`,敘事散文的數字完全不驗(兩段式 + Google grounding 下最實際的幻覺漏洞)。新 validator:regex 抽「代號/名稱 + 數字 + 單位」三元組,對照 `features.tw.stocks[sym]` 真值(容忍 ±5% 或四捨五入差),超差標 warning、**不阻斷**
- **驗收**:10 句正確 + 10 句捏造的測試集 → 捏造句 warning ≥8/10、誤報 ≤1/10;晨報實跑 guardrail summary 含 narrative_number 統計

### WP3.4 prompt token 預算 + 格式化段降溫
- **類型**:直接改|**規模**:S|**依賴**:WP3.1
- **要點**:`prompts.py` features JSON 序列化前按段落配額截斷(stocks 依 amount 排序取前 N、news 每則截 200 字,總預算常數化);`gemini_client._generate_json` 溫度參數化,兩段式第②段格式化呼叫 0.4 → 0.1
- **驗收**:餵 3 倍預算 features → prompt ≤ 預算且 watchlist 候選未被截;格式化 payload 溫度 = 0.1

### WP3.5 高風險模組測試補完
- **類型**:直接改(執行 specs/004、009、002 既列測試任務)|**規模**:M|**依賴**:WP0.1,隨時可做
- **範圍**(只挑高風險,不求覆蓋率):guardrails symbol guard(fail-closed)與清理邏輯、`cost/tracker.cost_of_usage`(grounded 與 cached token 計價,對照官方價目手算案例)、`storage/local_store._upsert_parquet` 去重與 `purge_future_rows`
- **驗收**:各模組 ≥5 個測試;全套 `pytest -q` <60s

---

## 依賴總覽與建議批次

```
Phase 0: WP0.1 → WP0.2 → {WP0.3, WP0.4}          (安全網 + 真實基線)
Phase 1: WP1.1 → WP1.2;WP1.3、WP1.4 並行(1.3 須在 1.2 重訓前)
Phase 2: WP2.1 → WP2.2 → WP2.3;WP2.4 子項逐個
Phase 3: WP3.1 → {WP3.2, WP3.4} → WP3.3;WP3.5 隨時
```

新開 spec:`017-adjusted-prices`(WP1.1+1.2)、`019-score-fusion`(WP2.2+2.3)、`020-feature-batch`(WP2.4)、`021-narrative-number-guard`(WP3.3)。
Converge 既有 spec:WP0.3(016)、WP3.2(004/008)、WP3.5(004/009/002)。其餘直接改。

## 驗證方式(每 WP 通用)

1. `pytest -q` 全綠(Phase 0 建立後為硬性前置)
2. 準確度 WP:`python -m reports.eval_snapshot --compare <baseline> <new>` 的 delta 表進 commit/PR 描述
3. 涉及 serving 的 WP:實跑一次晨報(`POST /brief/morning`)確認報告正常產出、guardrail summary 無異常、顯示價格為名目價

## 關鍵檔案索引

- 訓練/回測:`backend/reports/training_set.py`(499 行)、`backend/reports/backtest.py`(440 行,FEATURE_COLUMNS:157)、`backend/reports/strategy_calibration.py`(1413 行)
- 儲存/serving:`backend/storage/local_store.py`、`backend/reports/morning_brief.py`(打分套用:352-388)
- 特徵/資料:`backend/processor/{tw_features,fundamentals,fundamentals_history,market_regime}.py`、`backend/data_sources/{finmind_loader,taifex_loader,backfill_tw}.py`
- LLM/guardrail:`backend/ai/{gemini_client,prompts}.py`、`backend/guardrails/verify.py`、`backend/api/ask.py`
- 離線實驗:`qlib_offline/{run,dump,common}.py`
- 全部 gate 閾值:`backend/config.py:109-135`
