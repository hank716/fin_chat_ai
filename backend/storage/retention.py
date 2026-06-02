"""本機保留/清理（M7，對齊 design_docs §10–§12/§23）。

守 LOCAL_STORAGE_BUDGET_GB：把舊的、可回補的本機檔案清掉，需要時再從 pCloud（或 FinMind 重抓）回補。
  - reports：本機只留最近 KEEP_REPORTS_LOCAL 篇（其餘已備份 pCloud，刪本機；查看舊報告時自動回補）。
  - 清單外即時查的台股 parquet（adhoc 快取）：超過 ADHOC_TTL_DAYS 未更新就刪（可隨時再抓）。
回報清理結果 + 容量現況。失敗只記 log，不丟例外。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import universe
from config import settings
from storage.storage_monitor import local_storage_report

logger = logging.getLogger("ai-market-backend.retention")

REPORTS_DIR = Path(settings.local_storage_path) / "reports"
PARQUET_ROOT = Path(settings.local_storage_path) / "local_parquet"

KEEP_REPORTS_LOCAL = 90          # 本機保留最近幾篇報告（~3 個月日報）
ADHOC_PARQUET_TTL_DAYS = 14      # 清單外即時查標的 parquet 超過幾天未更新就清


def _evict_old_reports() -> int:
    if not REPORTS_DIR.exists():
        return 0
    jsons = sorted(REPORTS_DIR.glob("morning_*.json"))
    old = jsons[:-KEEP_REPORTS_LOCAL] if len(jsons) > KEEP_REPORTS_LOCAL else []
    removed = 0
    for f in old:
        rid = f.stem
        f.unlink(missing_ok=True)
        (REPORTS_DIR / f"{rid}.md").unlink(missing_ok=True)
        removed += 1
    return removed


def _prune_adhoc_parquet() -> int:
    keep = set(universe.watchlist_symbols()) | {"TWII"}
    cutoff = time.time() - ADHOC_PARQUET_TTL_DAYS * 86400
    pruned = 0
    for sub in (PARQUET_ROOT / "tw", PARQUET_ROOT / "tw" / "_chip", PARQUET_ROOT / "tw" / "_margin"):
        if not sub.exists():
            continue
        for p in sub.glob("*.parquet"):
            if p.stem in keep:
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    pruned += 1
            except OSError:
                pass
    return pruned


def enforce_retention() -> dict[str, Any]:
    summary = {
        "reports_evicted": _evict_old_reports(),
        "adhoc_parquet_pruned": _prune_adhoc_parquet(),
    }
    rep = local_storage_report()
    summary["used_gb"] = rep["used_gb"]
    summary["budget_gb"] = rep["budget_gb"]
    summary["alert_level"] = rep["alert_level"]
    logger.info("retention 完成: %s", summary)
    return summary
