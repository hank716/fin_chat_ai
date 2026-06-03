"""預抓基本面到磁碟快取（可選，對齊「用時間換運算資源」的設計）。

機器有開著時（或遠端喚醒後）先把「當天焦點股」的月營收/季財報暖進磁碟快取，之後的晨報
直接讀磁碟、幾乎不必再打 FinMind，把 ~225 FinMind calls 的尖峰從 08:30 移到 07:30。

關鍵：焦點股是**每天從全市場(2700+)動態篩出的 movers**，不是固定清單。所以預設模式
（dynamic-focus）走「跟晨報同一條路徑」：刷新全市場價/籌碼 → 篩 movers → 暖那些焦點股財報。
因 08:30 前台股未開盤、用的是前一交易日已結算資料，07:30 與 08:30 篩出的 movers 一致，
所以 07:30 暖到的正是 08:30 會用到的那批。

也可傳明確 symbols（或 --watchlist 用精選清單）做手動暖機。
CLI：
  python -m processor.prefetch_fundamentals                 # 只暖當天焦點股（快）
  python -m processor.prefetch_fundamentals --full          # 焦點優先，再慢慢掃全市場（可數小時，可中斷續跑）
  python -m processor.prefetch_fundamentals --full --minutes 50   # 本次只跑 50 分鐘，剩下下次續
  python -m processor.prefetch_fundamentals --watchlist     # 暖精選 watchlist
  python -m processor.prefetch_fundamentals --force         # 無視 TTL 全重抓
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from config import settings
from data_sources.finmind_loader import FinMindBackoff
from processor.fundamentals import build_fundamentals

logger = logging.getLogger("ai-market-backend.prefetch")


def _parse_hhmm_list(spec: str) -> list[int]:
    """'07:30,08:20' → [450, 500]（一日中的分鐘數）。略過不合法項目。"""
    out: list[int] = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            h, m = (int(x) for x in item.split(":"))
            out.append(h * 60 + m)
        except (ValueError, TypeError):
            continue
    return out


def _crawl_blackout_window() -> str:
    """慢爬靜默窗 'HH:MM-HH:MM'：顯式設定優先，否則連動晨報排程自動推算。

    自動推算：start = 最早晨報活動 − lead 分鐘；end = 最晚晨報活動 + tail 分鐘。
    晨報活動取 PREFETCH_TIMES + REPORT_TIMES（後者空則退回 morning_report_time）。
    推不出時間（皆未設）→ 回空字串＝不靜默。
    """
    explicit = (settings.fundamentals_crawl_blackout or "").strip()
    if explicit:
        return explicit
    times = _parse_hhmm_list(settings.prefetch_times)
    times += _parse_hhmm_list(settings.report_times or settings.morning_report_time)
    if not times:
        return ""
    start = (min(times) - settings.crawl_blackout_lead_min) % 1440
    end = (max(times) + settings.crawl_blackout_tail_min) % 1440
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def _in_crawl_blackout(window: str) -> bool:
    """現在（schedule_tz 當地時間）是否落在慢爬靜默窗 'HH:MM-HH:MM' 內。

    靜默窗用來把 FinMind 整點額度完整讓給晨報——FinMind 額度按小時回補，晨報前 1 小時
    停抓即可讓那小時的額度全歸晨報。窗格為空或格式不合法則一律不靜默（不擋慢爬）。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    window = (window or "").strip()
    if "-" not in window:
        return False
    try:
        a, b = window.split("-", 1)
        sh, sm = (int(x) for x in a.split(":"))
        eh, em = (int(x) for x in b.split(":"))
    except (ValueError, TypeError):
        return False
    now = datetime.now(ZoneInfo(settings.schedule_tz))
    cur = now.hour * 60 + now.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # 跨午夜的窗格

_WATCHLIST_JSON = Path(__file__).resolve().parent.parent / "configs" / "universe" / "tw.json"


def curated_watchlist() -> list[str]:
    """精選 watchlist 代號（tw.json 的 sectors 攤平去重）。讀不到回空清單。"""
    try:
        data = json.loads(_WATCHLIST_JSON.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取精選 watchlist %s 失敗: %s", _WATCHLIST_JSON, exc)
        return []
    return sorted({s for lst in data.get("sectors", {}).values() for s in lst})


def _dynamic_focus(refresh: bool) -> list[str]:
    """跟晨報同源：刷新全市場 → build_tw_features 篩 movers。

    build_tw_features 內部已對 focus 呼叫 build_fundamentals（會落地快取），
    所以這一步本身就暖了快取；回傳 focus 代號供後續計數/保險。
    """
    if refresh:
        try:
            from data_sources.backfill_tw_market import backfill_market
            backfill_market(days=3)
        except Exception as exc:  # noqa: BLE001 — 刷新失敗仍用既有 parquet 篩
            logger.warning("預抓：全市場刷新失敗，沿用既有資料: %s", exc)
    from processor import tw_features
    feats = tw_features.build_tw_features()
    return sorted(feats.get("stocks", {}).keys())


def _full_universe_focus_first(refresh: bool) -> list[str]:
    """全市場代號，但把當天焦點股(movers)排在最前面（優先暖）。"""
    focus = _dynamic_focus(refresh)
    import universe
    seen = set(focus)
    rest = [s for s in sorted(universe.watchlist_symbols()) if s not in seen]
    return focus + rest


def prefetch(symbols: list[str] | None = None, *, force: bool = False, refresh: bool = True,
             scope: str = "focus", max_seconds: float | None = None) -> dict[str, Any]:
    """暖基本面磁碟快取。回 {mode, total, ok, empty, error, skipped, done, elapsed_sec, force}。

    scope:
      "focus"（預設）→ 只暖當天焦點股(movers)，與晨報同源、快，08:30 前先暖好。
      "full"          → 焦點股優先，再「慢慢」掃全市場(2700+)。財報是季更慢資料，
                        命中未過期就跳過（幾乎 0 成本），所以本爬蟲**可重入/可續跑**：
                        中途睡眠/關機都沒關係，下次再跑會自動跳過已抓的、接著抓沒抓的。
      傳入 symbols     → 只暖那批（手動/精選），忽略 scope。
    max_seconds: 本次最多跑多久（給 full 分段續跑用；到時間就停，剩下下次再續）。
    FinMind 節流由 rate_limiter 自動處理（0.5 req/s）；full 全市場約需數小時～23h，刻意慢。
    """
    t0 = time.monotonic()
    if symbols is not None:
        mode, syms = "explicit", symbols
    elif scope == "full":
        mode, syms = "full", _full_universe_focus_first(refresh)
    else:
        mode, syms = "focus", _dynamic_focus(refresh)

    # 靜默窗只套用在全市場慢爬；focus/explicit（晨報自己的 prefetch）刻意在窗內執行，不可被擋。
    blackout = _crawl_blackout_window() if mode == "full" else ""
    ok = empty = error = processed = 0
    stop_reason: str | None = None
    for sym in syms:
        if max_seconds is not None and (time.monotonic() - t0) > max_seconds:
            stop_reason = "time_budget"
            break
        if blackout and _in_crawl_blackout(blackout):
            stop_reason = "brief_blackout"
            logger.info("慢爬進入晨報靜默窗 %s，本輪暫停（已處理 %d 檔），下輪續跑", blackout, processed)
            break
        processed += 1
        try:
            fu = build_fundamentals(sym, force=force)
            if fu:
                ok += 1
            else:
                empty += 1
        except FinMindBackoff as exc:
            # 額度耗盡 / IP 封鎖：再打也只是繼續失敗。停手，靠磁碟快取下輪重入續跑。
            stop_reason = "finmind_backoff"
            logger.warning("FinMind 額度/封鎖觸發，慢爬本輪停止（已處理 %d 檔，下輪續跑）: %s",
                           processed, exc)
            break
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不中斷整批
            error += 1
            logger.warning("預抓基本面 %s 失敗: %s", sym, exc)
    result = {"mode": mode, "total": len(syms), "processed": processed, "ok": ok,
              "empty": empty, "error": error, "skipped": len(syms) - processed,
              "done": stop_reason is None and processed == len(syms),
              "stop_reason": stop_reason,
              "elapsed_sec": round(time.monotonic() - t0, 1), "force": force}
    logger.info("基本面預抓完成: %s", result)
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    argv = sys.argv[1:]
    force = "--force" in argv
    use_watchlist = "--watchlist" in argv
    scope = "full" if "--full" in argv else "focus"

    max_seconds = None
    if "--minutes" in argv:
        i = argv.index("--minutes")
        max_seconds = float(argv[i + 1]) * 60  # 後面那個值是分鐘數
        argv = argv[:i] + argv[i + 2:]         # 移除 --minutes 與其值，避免被當成 limit

    positional = [a for a in argv if not a.startswith("--")]
    limit = int(positional[0]) if positional and positional[0].isdigit() else None

    symbols = None
    if use_watchlist:
        scope = "focus"  # explicit 模式忽略 scope
        symbols = curated_watchlist()
        if limit:
            symbols = symbols[:limit]
    print(json.dumps(prefetch(symbols, force=force, scope=scope, max_seconds=max_seconds),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
