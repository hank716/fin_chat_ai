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
from ai import gemini_client
from ai.llm_client import get_llm_client
from cost import tracker
from data_sources import news_loader, yfinance_loader
from data_sources.backfill_tw_market import backfill_market
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


def _caution_signals(entry: dict[str, Any]) -> list[str]:
    """從個股 features 萃取可佐證『偏空/要注意』的實際訊號（皆為真實數值，不外推）。"""
    sig: list[str] = []
    r5 = entry.get("return_5d_pct")
    if isinstance(r5, (int, float)) and r5 < 0:
        sig.append(f"近5日{r5:.1f}%")
    vs = entry.get("vs_index_20d_pct")
    if isinstance(vs, (int, float)) and vs < 0:
        sig.append(f"相對大盤{vs:.1f}%")
    if entry.get("above_ma20") is False:
        sig.append("跌破MA20")
    streak = entry.get("foreign_net_streak")
    if isinstance(streak, int) and streak < 0:
        sig.append(f"外資連賣{abs(streak)}日")
    f5 = entry.get("foreign_net_buy_5d_lots")
    if isinstance(f5, (int, float)) and f5 < 0:
        sig.append(f"外資5日賣超{abs(int(f5)):,}張")
    smr = entry.get("short_margin_ratio_pct")
    if isinstance(smr, (int, float)) and smr >= 30:
        sig.append(f"資券比{smr:.0f}%偏高")
    return sig


def _backfill_caution(result: Any, features: dict[str, Any], want: int = 5) -> int:
    """偏空清單不足時，從 movers 以實際數據補齊，確保晨報永遠有『偏空/要注意標的』段落。

    模型在偏多盤勢常整段略過 tw_caution（rare bug）。這裡用 movers 跌幅/資券比/相對弱勢/外資賣超排行
    補列——符號都出自 movers（必在 features.tw.stocks，可過 guardrail），signals 全為真實數值不捏造。
    回補上的檔數。
    """
    from ai.schemas import WatchItem

    if len(result.tw_caution) >= want:
        return 0
    tw = features.get("tw", {}) or {}
    stocks = tw.get("stocks", {}) or {}
    movers = tw.get("movers", {}) or {}
    have = {w.symbol for w in result.tw_caution}
    added = 0
    for key in ("top_losers_5d", "top_below_index_20d", "top_short_margin_ratio", "top_foreign_sell_5d"):
        for it in movers.get(key, []):
            if len(result.tw_caution) >= want:
                break
            sym = it.get("symbol")
            if not sym or sym in have:
                continue
            entry = stocks.get(sym, {})
            sigs = _caution_signals(entry)
            if not sigs:  # 找不到可佐證的偏空訊號就不硬湊
                continue
            have.add(sym)
            added += 1
            result.tw_caution.append(WatchItem(
                symbol=sym,
                name=entry.get("name") or it.get("name") or sym,
                sector=entry.get("sector") or it.get("sector"),
                thesis="技術面偏空：" + "、".join(sigs[:3])
                + "（系統依排行自動補列，建議搭配當日量價與消息確認）",
                signals=sigs,
                uncertainty="自動補列、未經 AI 個別研判；目標價/止損請依技術區間自行評估。",
            ))
    if added:
        logger.info("tw_caution 由模型回傳 %d 檔，已用 movers 實際數據補至 %d 檔",
                    len(result.tw_caution) - added, len(result.tw_caution))
    return added


def _build_combined_features(refresh_tw: bool) -> tuple[dict[str, Any], dict[str, int]]:
    """組出餵 Gemini 的合併 features：美股+加密 / 台股+籌碼 / 聚焦新聞 / 跨市場連動。"""
    # 1) 美股指數 + BTC + 大盤 TWII → DQ → parquet
    landed: dict[str, int] = {}
    for market, rows in yfinance_loader.fetch_intermarket().items():
        res = local_store.write_prices(_dq_filter(rows), market)
        landed[market] = res["symbols"]

    # 2) 台股全市場刷新（近 3 交易日，TWSE/TPEx 單日端點，快）
    if refresh_tw:
        try:
            backfill_market(days=3)
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
    raw_query: str | None = None, *, refresh_tw: bool = True,
    push_discord: bool = False, publish: bool = False,
) -> dict[str, Any]:
    generated_at = datetime.now(ZoneInfo(settings.tz))

    feats, landed = _build_combined_features(refresh_tw)

    # 晨報主推理即時連網：兩段式 ①PRO+Google搜尋 寫分析稿 → ②Flash 純格式化成結構
    result, research_usage, struct_usage = gemini_client.analyze_full_brief_grounded(feats)
    brief_cost = round(
        # ①研究階段 grounded（PRO+Google 搜尋）②格式化階段純文字（Flash，不連網）
        tracker.cost_of_usage(research_usage, settings.gemini_model_brief, grounded=True)
        + tracker.cost_of_usage(struct_usage, settings.gemini_model_qa),
        4,
    )
    usage = {
        "input_tokens": research_usage["input_tokens"] + struct_usage["input_tokens"],
        "output_tokens": research_usage["output_tokens"] + struct_usage["output_tokens"],
    }
    tracker.record_cost(brief_cost)

    # 偏空清單偶爾被模型整段略過 → 用 movers 實際數據補齊（在 guardrail 前，符號必在資料範圍內）
    _backfill_caution(result, feats)

    cost_info = {
        "brief_twd": brief_cost,
        "tokens": usage,
        "month": tracker.current_month(),
        "month_total_twd": tracker.month_total(),       # 全站本月累計（晨報+所有問答）
        "day_total_twd": tracker.today_total(),          # 全站今日累計
        "monthly_limit_twd": float(settings.monthly_cost_limit_twd),
    }
    logger.info("晨報 Gemini 花費 NT$%.4f（本月累計 NT$%.2f）",
                brief_cost, cost_info["month_total_twd"])

    # guardrail：驗證未超出資料範圍、無捏造/禁語，清理後再 render
    result, guardrail = run_guardrails(result, feats)
    logger.info("guardrail passed=%s errors=%s warnings=%s",
                guardrail["passed"], guardrail["error_count"], guardrail["warning_count"])

    # 4) builders
    report = build_report_dict(result, generated_at=generated_at, raw_query=raw_query)
    report["guardrail"] = guardrail
    report["cost"] = cost_info
    report_id = f"morning_{generated_at:%Y%m%d_%H%M%S}"
    report["report_id"] = report_id
    report["report_date"] = generated_at.date().isoformat()  # 晨報日期(今日)，有別於資料日期
    report["features"] = feats
    report["landed_symbols"] = landed
    report["markdown"] = build_markdown(
        result, generated_at=generated_at, raw_query=raw_query, cost=cost_info
    )
    report["copy_for_ai"] = build_copy_for_ai(
        result, generated_at=generated_at, raw_query=raw_query
    )

    # 5) 存檔
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / f"{report_id}.json"
    md_path = REPORTS_DIR / f"{report_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(report["markdown"], encoding="utf-8")
    logger.info("morning brief %s 產生完成 (landed=%s)", report_id, landed)

    # 6) Discord 推送
    pushed = False
    if push_discord:
        from notify.discord import send_daily_summary
        pushed = send_daily_summary(report)

    # 7) 發布：pCloud 冷備份 + Supabase report_index 暖索引
    if publish:
        from publish import pcloud_backup, supabase_publish
        pcloud_paths = pcloud_backup.backup_report(json_path, md_path)
        supabase_publish.publish_report_index(
            report, pcloud_paths=pcloud_paths, discord_pushed=pushed
        )
        # 8) 保留/清理：守本機容量預算（舊報告已備份 pCloud，可回補）
        try:
            from storage.retention import enforce_retention
            enforce_retention()
        except Exception as exc:  # noqa: BLE001
            logger.warning("retention 失敗: %s", exc)

    return report


def _safe_path(report_id: str, suffix: str) -> Path | None:
    if not REPORT_ID_RE.match(report_id):
        return None
    return REPORTS_DIR / f"{report_id}{suffix}"


def load_report(report_id: str) -> dict[str, Any] | None:
    p = _safe_path(report_id, ".json")
    if p is None:
        return None
    if not p.exists():
        # 本機已清掉（retention 汰換）→ 從 pCloud 冷儲存回補
        try:
            from publish.pcloud_backup import restore_report
            restore_report(report_id)
        except Exception:  # noqa: BLE001
            pass
    if not p.exists():
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


def list_reports(limit: int = 120) -> list[dict[str, Any]]:
    """歷史報告清單（新到舊），給首頁列表用。只取輕量欄位。"""
    if not REPORTS_DIR.exists():
        return []
    files = sorted(REPORTS_DIR.glob("morning_*.json"), reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 壞檔跳過
            continue
        out.append({
            "report_id": d.get("report_id", f.stem),
            "report_type": d.get("report_type", "每日跨市場晨報"),
            "report_date": d.get("report_date"),
            "data_as_of": d.get("data_as_of"),
            "generated_at": d.get("generated_at"),
            "headline": d.get("headline", ""),
        })
    return out


def report_date_exists(d: date) -> bool:
    """當日（report_id 前綴 morning_YYYYMMDD）是否已有報告。給 scheduler catch-up 用。"""
    if not REPORTS_DIR.exists():
        return False
    return any(REPORTS_DIR.glob(f"morning_{d:%Y%m%d}_*.json"))
