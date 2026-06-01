"""每日跨市場晨報管線（M1 step 5）。

跑完整黃金路徑：yfinance 抓 → parquet 落地 → intermarket features → Gemini 結構化分析
→ md/json/copy-for-ai → 存檔 storage/reports/{id}.json|.md。

M1 範圍：美股指數 + BTC。台股/籌碼/新聞/guardrail 後續里程碑加入。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from ai.llm_client import get_llm_client
from data_sources import yfinance_loader
from data_sources.ingest import _dq_filter
from processor.intermarket_features import build_intermarket_features
from reports.copy_for_ai_builder import build_copy_for_ai
from reports.json_builder import build_report_dict
from reports.markdown_builder import build_markdown
from storage import local_store

logger = logging.getLogger("ai-market-backend.morning_brief")

REPORTS_DIR = Path(settings.local_storage_path) / "reports"
REPORT_ID_RE = re.compile(r"^morning_\d{8}_\d{6}$")


def generate_morning_brief(raw_query: str | None = None) -> dict[str, Any]:
    generated_at = datetime.now(ZoneInfo(settings.tz))

    # 1) 抓美股指數 + BTC → DQ → parquet
    landed: dict[str, int] = {}
    for market, rows in yfinance_loader.fetch_intermarket().items():
        res = local_store.write_prices(_dq_filter(rows), market)
        landed[market] = res["symbols"]

    # 2) intermarket features
    feats = build_intermarket_features()

    # 3) Gemini 結構化分析
    result = get_llm_client().analyze_intermarket(feats)

    # 4) builders
    report = build_report_dict(result, generated_at=generated_at, raw_query=raw_query)
    report_id = f"morning_{generated_at:%Y%m%d_%H%M%S}"
    report["report_id"] = report_id
    report["features"] = feats
    report["landed_symbols"] = landed
    report["markdown"] = build_markdown(result, generated_at=generated_at, raw_query=raw_query)
    report["copy_for_ai"] = build_copy_for_ai(
        result, generated_at=generated_at, raw_query=raw_query
    )

    # 5) 存檔
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"{report_id}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (REPORTS_DIR / f"{report_id}.md").write_text(report["markdown"], encoding="utf-8")
    logger.info("morning brief %s 產生完成 (landed=%s)", report_id, landed)
    return report


def _safe_path(report_id: str, suffix: str) -> Path | None:
    if not REPORT_ID_RE.match(report_id):
        return None
    return REPORTS_DIR / f"{report_id}{suffix}"


def load_report(report_id: str) -> dict[str, Any] | None:
    p = _safe_path(report_id, ".json")
    if p is None or not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def load_markdown(report_id: str) -> str | None:
    p = _safe_path(report_id, ".md")
    if p is None or not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def latest_report_id() -> str | None:
    if not REPORTS_DIR.exists():
        return None
    files = sorted(REPORTS_DIR.glob("morning_*.json"))
    return files[-1].stem if files else None
