"""歷史行情慢爬：把 parquet 歷史回溯拉長到 N 年，餵大 edge 訓練集（不影響晨報）。

兩條獨立軌道（不同 provider、可並存、皆冪等可續跑）：

  軌道 A  crawl_listed_history：上市(TWSE) 價 + 上市/上櫃 籌碼/融資券。走 TWSE/TPEx「單日=全市場」
          端點（皆支援歷史、**不打 FinMind**，limiter 已壓 1 req/s）。游標由現有 parquet 最早日往更舊
          逐 30 天 chunk 回補，達 target 即止。約 490 交易日 × ~5 req ≈ ~40 分鐘，一晚跑完。

  軌道 B  crawl_tpex_prices：上櫃(TPEx) 個股「價」。官方端點不給歷史 → FinMind per-stock
          （一檔一次拿 N 年）。每小時小批（預設 120 檔，<550/h 留額度給晨報）；進晨報靜默窗跳過、
          遇 FinMind 額度耗盡即停、已完成記進 done 集、下個整點續跑。電腦常開→大半天爬完 ~1100 檔。

進度落地**各軌道一個檔**（避免兩軌道並行時 last-writer-wins 互相覆蓋）：
  storage/strategy/history_listed.json  /  history_tpex.json
同軌道的並行由端點層單例 mutex（api.brief._history_state）擋住。

安全：只寫舊日期（local_store future-cutoff 仍擋未來列、as_of 不變）→ 不影響每日晨報。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from data_sources import rate_limiter
from data_sources.backfill_tw_market import backfill_window
from data_sources.ingest import TW_MARKET, _dq_filter
from processor.prefetch_fundamentals import _crawl_blackout_window, _in_crawl_blackout
from storage import local_store

logger = logging.getLogger("ai-market-backend.history_crawl")

# 用來偵測「現有 parquet 最早日」的探針：取一檔必定存在且會被軌道 A 回補的上市權值股（台積電）。
# 避免用大盤 TWII（非 TWSE 個股端點回補對象，歷史不會被軌道 A 延長）。
_EARLIEST_PROBE = "2330"
_STRATEGY_DIR = Path(settings.local_storage_path) / "strategy"
LISTED_PATH = _STRATEGY_DIR / "history_listed.json"
TPEX_PATH = _STRATEGY_DIR / "history_tpex.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(path: Path, p: dict[str, Any]) -> None:
    p["updated_at"] = datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")


def _target_earliest() -> date:
    return date.today() - timedelta(days=settings.history_crawl_target_days)


def _parquet_earliest() -> date | None:
    """現有 parquet 的最早交易日（用台積電 2330 當代表，會被軌道 A 延長）。無資料回 None。"""
    import pandas as pd
    df = local_store.read_prices(_EARLIEST_PROBE, TW_MARKET)
    if df.empty:
        return None
    return pd.to_datetime(df["trade_date"]).min().date()


def _post_crawl_refresh() -> None:
    """有新歷史落地後，重建訓練集 + 重訓 edge 模型，讓新資料立刻進模型。失敗不阻斷。"""
    try:
        from reports import strategy_calibration, training_set
        training_set.build_training_set()
        strategy_calibration.train_edge_model()
        strategy_calibration.rebuild()
    except Exception as exc:  # noqa: BLE001
        logger.warning("慢爬後重建訓練集/模型失敗（不影響爬蟲）: %s", exc)


# ───────────────────────── 軌道 A：上市歷史（TWSE，不碰 FinMind）─────────────────────────

def crawl_listed_history(max_seconds: float | None = None) -> dict[str, Any]:
    """上市歷史回補：游標往更舊逐 chunk 回補到 target。回 stats。"""
    if max_seconds is None:
        max_seconds = settings.history_crawl_max_minutes * 60
    p = _load(LISTED_PATH)
    target = _target_earliest()
    cursor = (date.fromisoformat(p["earliest_done"]) if p.get("earliest_done")
              else (_parquet_earliest() or date.today()))
    blackout = _crawl_blackout_window()
    t0 = time.monotonic()
    chunks = 0
    stop: str | None = None
    while cursor > target:
        if max_seconds and (time.monotonic() - t0) > max_seconds:
            stop = "time_budget"
            break
        if blackout and _in_crawl_blackout(blackout):
            stop = "brief_blackout"
            break
        chunk_end = cursor - timedelta(days=1)
        chunk_start = max(target, chunk_end - timedelta(days=settings.history_crawl_chunk_days))
        try:
            backfill_window(chunk_start, chunk_end)               # 不含今日 TPEx 價
        except Exception as exc:  # noqa: BLE001 — 單 chunk 失敗就停、游標已存、下輪續
            stop = f"error:{exc}"
            logger.warning("上市歷史回補 chunk [%s,%s] 失敗: %s", chunk_start, chunk_end, exc)
            break
        cursor = chunk_start
        p["earliest_done"] = cursor.isoformat()
        p["target_earliest"] = target.isoformat()
        p["chunks_done"] = p.get("chunks_done", 0) + 1
        chunks += 1
        _save(LISTED_PATH, p)
    reached = cursor <= target
    p["reached_target"] = reached
    _save(LISTED_PATH, p)
    if chunks:
        _post_crawl_refresh()
    result = {"track": "listed", "chunks": chunks, "earliest_done": cursor.isoformat(),
              "target": target.isoformat(), "reached_target": reached, "stop_reason": stop,
              "elapsed_sec": round(time.monotonic() - t0, 1)}
    logger.info("上市歷史慢爬: %s", result)
    return result


# ───────────────────────── 軌道 B：上櫃個股價（FinMind，每小時小批）─────────────────────────

def _tpex_symbols(p: dict[str, Any]) -> list[str]:
    """上櫃 symbol 清單（FinMind type=='tpex' ∩ universe）。快取進進度檔，避免每輪重打 FinMind。"""
    cached = p.get("symbols")
    if cached:
        return cached
    from data_sources import finmind_loader
    import universe
    info = finmind_loader.get_taiwan_stock_info()        # 1 次 FinMind call
    tpex = {r["stock_id"] for r in info if r.get("type") == "tpex"}
    keep = set(universe.watchlist_symbols())
    syms = sorted(tpex & keep)
    p["symbols"] = syms
    return syms


def crawl_tpex_prices(max_calls: int | None = None) -> dict[str, Any]:
    """上櫃個股價慢爬：每輪做尚未完成的前 max_calls 檔（FinMind per-stock）。回 stats。

    嚴守 FinMind 額度：每輪上限 max_calls（<550/h）+ 晨報靜默窗跳過 + 遇額度耗盡/封鎖即停、下輪續。
    """
    if max_calls is None:
        max_calls = settings.history_finmind_per_run
    blackout = _crawl_blackout_window()
    if blackout and _in_crawl_blackout(blackout):
        return {"track": "tpex_prices", "crawled": 0, "stop_reason": "brief_blackout"}

    p = _load(TPEX_PATH)
    target = _target_earliest()
    try:
        syms = _tpex_symbols(p)
    except Exception as exc:  # noqa: BLE001 — 連清單都拿不到（額度/網路）就退避
        return {"track": "tpex_prices", "crawled": 0, "stop_reason": f"info_failed:{exc}"}
    p["total"] = len(syms)
    p["target_earliest"] = target.isoformat()
    done = set(p.get("done", []))
    todo = [s for s in syms if s not in done]

    from data_sources.finmind_loader import FinMindBackoff, fetch_stock_prices_normalized

    start_s, end_s = target.isoformat(), date.today().isoformat()
    crawled = ok = empty = err = 0
    stop: str | None = None
    t0 = time.monotonic()
    for sym in todo:
        if crawled >= max_calls:
            stop = "batch_limit"
            break
        if blackout and _in_crawl_blackout(blackout):
            stop = "brief_blackout"
            break
        crawled += 1
        try:
            rows = _dq_filter(fetch_stock_prices_normalized(sym, start_s, end_s))
            if rows:
                local_store.write_prices(rows, TW_MARKET)
                ok += 1
            else:
                empty += 1
            done.add(sym)                                  # 成功（含查無資料）→ 標記完成
        except (FinMindBackoff, rate_limiter.RateLimitExhausted) as exc:
            stop = "finmind_backoff"                        # 額度耗盡/封鎖：立即停手、下個整點續
            logger.warning("上櫃價慢爬遇 FinMind 退避，本輪停止（已完成 %d/%d 檔）: %s",
                           len(done), len(syms), exc)
            break
        except Exception as exc:  # noqa: BLE001 — 單檔其他錯誤：標記完成避免卡關
            err += 1
            done.add(sym)
            logger.warning("上櫃價慢爬 %s 失敗（跳過）: %s", sym, exc)

    p["done"] = sorted(done)
    _save(TPEX_PATH, p)
    remaining = len(syms) - len(done)
    if ok and remaining == 0:                              # 全部上櫃爬完才重建（避免每小時重建）
        _post_crawl_refresh()
    result = {"track": "tpex_prices", "crawled": crawled, "ok": ok, "empty": empty, "error": err,
              "done_total": len(done), "remaining": remaining, "stop_reason": stop,
              "elapsed_sec": round(time.monotonic() - t0, 1)}
    logger.info("上櫃價慢爬: %s", result)
    return result


def status() -> dict[str, Any]:
    """目前慢爬進度（給端點/觀察用）。"""
    listed = _load(LISTED_PATH)
    tpex = _load(TPEX_PATH)
    return {
        "target_earliest": _target_earliest().isoformat(),
        "parquet_earliest": (_parquet_earliest() or date.today()).isoformat(),
        "listed": {k: listed.get(k) for k in
                   ("earliest_done", "target_earliest", "chunks_done", "reached_target", "updated_at")},
        "tpex_prices": {"total": tpex.get("total"), "done": len(tpex.get("done", [])),
                        "remaining": (tpex.get("total") or 0) - len(tpex.get("done", [])),
                        "updated_at": tpex.get("updated_at")},
    }
