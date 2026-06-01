"""Local storage capacity monitor（移植 finflow_ai/services/storage_monitor.py，改寫成 parquet）。

finflow 版監控 Postgres + Qlib dataset + model artifacts；本專案無 Postgres，
改測 **local parquet SSOT 目錄**佔用 vs env 預算 `LOCAL_STORAGE_BUDGET_GB`。

回報兩種視角：
  **footprint**（使用者實際關心）：storage/ 下各 parquet/report 子目錄之和 vs budget。
  **host disk**（主機系統視角）：total/used/free。

警示分兩層：
  footprint 預算：< 70% ok / 70–100% warning / ≥ 100% critical
  主機磁碟    ：< 15 GB free critical / < 30 GB free warning
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from config import settings

STORAGE_ROOT = Path(settings.local_storage_path)

# storage/ 下要計入 footprint 的子目錄（對齊 design_docs §28 storage layout）
COMPONENT_DIRS = ["local_parquet", "features", "reports", "raw", "cache", "logs"]

# 主機磁碟警示閾值
WARNING_FREE_GB = 30
CRITICAL_FREE_GB = 15

# heuristic：假設每交易日新增約 80 MB（prices + chip + features 累積）
ASSUMED_DAILY_GROWTH_MB = 80


def _dir_size(path: Path) -> int:
    """遞迴算目錄實際佔用磁碟空間 (bytes)；目錄不存在回 0。

    用 st_blocks×512（實際配置的磁碟區塊, 同 `du`），不是 st_size：
    大量小檔的 block rounding 讓實際佔用遠大於內容總和，容量監控要看真實消耗。
    """
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_blocks * 512
            except OSError:
                # broken symlink / race; 跳過
                pass
    return total


def local_storage_report() -> dict[str, Any]:
    """組出本機容量報告（footprint vs budget + host disk）。"""
    components = {name: _dir_size(STORAGE_ROOT / name) for name in COMPONENT_DIRS}
    used_bytes = sum(components.values())

    budget_gb = float(settings.local_storage_budget_gb)
    budget_bytes = int(budget_gb * 1024**3)
    used_gb = used_bytes / 1024**3
    used_pct_of_budget = (
        round(used_bytes / budget_bytes * 100, 2) if budget_bytes else 0.0
    )
    if budget_bytes and used_bytes >= budget_bytes:
        footprint_alert_level = "critical"
    elif budget_bytes and used_bytes >= budget_bytes * 0.7:
        footprint_alert_level = "warning"
    else:
        footprint_alert_level = "ok"

    probe = STORAGE_ROOT if STORAGE_ROOT.exists() else Path("/")
    usage = shutil.disk_usage(str(probe))
    free_gb = usage.free / 1024**3

    if free_gb < CRITICAL_FREE_GB:
        host_alert_level = "critical"
    elif free_gb < WARNING_FREE_GB:
        host_alert_level = "warning"
    else:
        host_alert_level = "ok"

    # 整體 alert：兩者取最嚴重
    _levels = {"ok": 0, "warning": 1, "critical": 2}
    worst = max(_levels[footprint_alert_level], _levels[host_alert_level])
    overall_alert_level = {0: "ok", 1: "warning", 2: "critical"}[worst]

    daily_growth_bytes = ASSUMED_DAILY_GROWTH_MB * 1024**2
    estimated_days_remaining = (
        int(usage.free / daily_growth_bytes) if daily_growth_bytes else None
    )

    return {
        # footprint（使用者關心的核心數字）
        "used_bytes": used_bytes,
        "used_gb": round(used_gb, 3),
        "budget_gb": budget_gb,
        "used_pct_of_budget": used_pct_of_budget,
        "footprint_alert_level": footprint_alert_level,
        "components": components,
        # host disk
        "disk_total_bytes": usage.total,
        "disk_used_bytes": usage.used,
        "disk_free_bytes": usage.free,
        "disk_used_pct": round(usage.used / usage.total * 100, 2) if usage.total else 0.0,
        "host_alert_level": host_alert_level,
        # 整體（footprint 超標 OR 主機快滿 都 raise）
        "alert_level": overall_alert_level,
        "estimated_days_remaining": estimated_days_remaining,
        "estimate_basis": f"heuristic: free / {ASSUMED_DAILY_GROWTH_MB} MB per trading day",
        "thresholds": {
            "budget_gb": budget_gb,
            "warning_free_gb": WARNING_FREE_GB,
            "critical_free_gb": CRITICAL_FREE_GB,
        },
    }
