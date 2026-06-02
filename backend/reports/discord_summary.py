"""Discord 晨報短摘要（M4，對齊 design_docs §6.3）。

從 report dict（含 features）組出 Discord 用的簡短重點＋Web 連結（不放長文）。
Discord 單則上限 2000 字，這裡刻意精簡。
"""
from __future__ import annotations

from typing import Any

from config import settings


def _pct(v: Any) -> str:
    if v is None:
        return "—"
    return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"


def _report_url(report_id: str) -> str:
    base = (settings.public_report_base_url or "").rstrip("/")
    if base:
        return f"{base}/report/{report_id}"
    host = (settings.public_hostname or "").strip()
    if host:
        return f"https://{host}/report/{report_id}"
    return f"/report/{report_id}"  # 未設對外網址時退回相對路徑


def _line_items(items: list[dict], key: str, fmt) -> list[str]:
    return [f"- {it.get('name', it.get('symbol'))}：{fmt(it.get(key))}" for it in items]


def build_discord_summary(report: dict[str, Any]) -> str:
    feats = report.get("features", {}) or {}
    assets = (feats.get("us_crypto", {}) or {}).get("assets", {}) or {}
    tw = feats.get("tw", {}) or {}
    movers = tw.get("movers", {}) or {}
    sectors = tw.get("sectors", {}) or {}

    parts: list[str] = []
    parts.append(f"📊 每日市場晨報｜{report.get('data_as_of', '')} 08:30")
    parts.append(f"\n**簡短結論**\n{report.get('headline', '')}")

    # 主要指數
    idx_lines = []
    for sym, label in [("NASDAQ", "Nasdaq"), ("SP500", "S&P 500"), ("SOX", "費半 SOX"),
                       ("DJI", "道瓊"), ("BTC", "BTC")]:
        a = assets.get(sym)
        if a:
            idx_lines.append(f"- {label}：{_pct(a.get('return_1d_pct'))}")
    twi = tw.get("index") or {}
    if twi:
        idx_lines.append(f"- 加權指數：{_pct(twi.get('return_1d_pct'))}")
    if idx_lines:
        parts.append("**主要指數（前一日）**\n" + "\n".join(idx_lines))

    # 台股強勢族群（近20日）
    strong = sorted(
        ((s, d.get("avg_return_20d_pct")) for s, d in sectors.items()
         if d.get("avg_return_20d_pct") is not None),
        key=lambda x: x[1], reverse=True,
    )[:4]
    if strong:
        parts.append("**台股觀察族群（近20日強）**\n"
                     + "\n".join(f"- {s}：{_pct(v)}" for s, v in strong))

    # 外資買賣超
    buy = movers.get("top_foreign_buy_5d", [])[:3]
    sell = movers.get("top_foreign_sell_5d", [])[:3]
    if buy:
        parts.append("**外資5日買超**\n"
                     + "\n".join(f"- {it['name']}：{it['foreign_net_buy_5d_lots']:,} 張" for it in buy))
    if sell:
        parts.append("**外資5日賣超**\n"
                     + "\n".join(f"- {it['name']}：{it['foreign_net_buy_5d_lots']:,} 張" for it in sell))

    # 候選 / 要注意
    watch = report.get("tw_watchlist", [])
    caution = report.get("tw_caution", [])
    if watch:
        parts.append("**候選觀察（正向）**\n"
                     + "\n".join(f"- {w['symbol']} {w['name']}" for w in watch))
    if caution:
        parts.append("**要注意（負向）**\n"
                     + "\n".join(f"- {w['symbol']} {w['name']}" for w in caution))

    parts.append(f"\n📄 完整報告：{_report_url(report.get('report_id', ''))}")
    gr = report.get("guardrail") or {}
    if gr:
        status = "✅ 通過" if gr.get("passed") and not gr.get("warning_count") else f"⚠️ {gr.get('error_count',0)}攔/{gr.get('warning_count',0)}警"
        parts.append(f"🛡️ Guardrail：{status}")
    parts.append("\n_此結果僅供家庭內部市場研究使用，不構成投資建議。_")

    text = "\n\n".join(parts)
    return text[:1990]  # Discord 2000 字上限留餘裕
