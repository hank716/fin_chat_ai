"""每日跨市場晨報管線（M1 step 5）。

跑完整黃金路徑：yfinance 抓 → parquet 落地 → intermarket features → Gemini 結構化分析
→ md/json/copy-for-ai → 存檔 storage/reports/{id}.json|.md。

M1 範圍：美股指數 + BTC。台股/籌碼/新聞/guardrail 後續里程碑加入。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import universe
from config import settings
from ai.llm_client import get_llm_client
from data_sources import news_loader, yfinance_loader
from data_sources.backfill_tw import backfill_watchlist
from data_sources.ingest import _dq_filter
from guardrails.verify import run_guardrails
from processor.intermarket_features import build_intermarket_features
from processor.tw_features import build_tw_features
from reports.copy_for_ai_builder import build_copy_for_ai
from reports.json_builder import build_report_dict
from reports.markdown_builder import build_markdown
from storage import local_store

logger = logging.getLogger("ai-market-backend.morning_brief")

REPORTS_DIR = Path(settings.local_storage_path) / "reports"
REPORT_ID_RE = re.compile(r"^morning_\d{8}_\d{6}$")


def _news_focus_symbols(tw_feats: dict[str, Any], cap: int = 12) -> list[str]:
    """從 movers 挑出最值得看新聞的台股（漲跌幅 + 外資買賣超前段），去重。"""
    out: list[str] = []
    movers = tw_feats.get("movers", {})
    for key in ("top_gainers_5d", "top_losers_5d", "top_foreign_buy_5d", "top_foreign_sell_5d"):
        for item in movers.get(key, []):
            sym = item.get("symbol")
            if sym and sym not in out:
                out.append(sym)
    return out[:cap]


def _build_combined_features(refresh_tw: bool) -> tuple[dict[str, Any], dict[str, int]]:
    """組出餵 Gemini 的合併 features：美股+加密 / 台股+籌碼 / 聚焦新聞 / 跨市場連動。"""
    # 1) 美股指數 + BTC + 大盤 TWII → DQ → parquet
    landed: dict[str, int] = {}
    for market, rows in yfinance_loader.fetch_intermarket().items():
        res = local_store.write_prices(_dq_filter(rows), market)
        landed[market] = res["symbols"]

    # 2) 台股價格 + 籌碼（每日刷新近一週，含上市/上櫃）
    if refresh_tw:
        try:
            backfill_watchlist(days=7)
        except Exception as exc:  # noqa: BLE001 — 刷新失敗仍用既有落地資料產報告
            logger.warning("台股每日刷新失敗，沿用既有資料: %s", exc)

    us_crypto = build_intermarket_features()
    tw = build_tw_features()
    # FinMind 新聞為單日語意：用資料日(as_of)與 today 當 start_date 才拿得到最新新聞
    news = news_loader.fetch_news(
        _news_focus_symbols(tw), as_of=tw.get("as_of"), per_symbol=2
    )

    combined = {
        "as_of": tw.get("as_of") or us_crypto.get("as_of"),
        "us_crypto": us_crypto,
        "tw": tw,
        "news": news,
        "linkage": universe.us_to_tw_linkage(),
    }
    return combined, landed


def generate_morning_brief(
    raw_query: str | None = None, *, refresh_tw: bool = True
) -> dict[str, Any]:
    generated_at = datetime.now(ZoneInfo(settings.tz))

    feats, landed = _build_combined_features(refresh_tw)

    # Gemini 完整敘事晨報
    result = get_llm_client().analyze_full_brief(feats)

    # guardrail：驗證未超出資料範圍、無捏造/禁語，清理後再 render
    result, guardrail = run_guardrails(result, feats)
    logger.info("guardrail passed=%s errors=%s warnings=%s",
                guardrail["passed"], guardrail["error_count"], guardrail["warning_count"])

    # 4) builders
    report = build_report_dict(result, generated_at=generated_at, raw_query=raw_query)
    report["guardrail"] = guardrail
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


def report_date_exists(d: date) -> bool:
    """當日（report_id 前綴 morning_YYYYMMDD）是否已有報告。給 scheduler catch-up 用。"""
    if not REPORTS_DIR.exists():
        return False
    return any(REPORTS_DIR.glob(f"morning_{d:%Y%m%d}_*.json"))
