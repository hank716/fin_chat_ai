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
    rdate = report.get("report_date") or report.get("data_as_of", "")
    parts.append(f"📊 每日市場晨報｜{rdate} 08:30（資料日 {report.get('data_as_of','')}）")
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

    # 外資買賣超：top_foreign_sell_5d 與 buy 共用同一 key（升冪），值為負＝賣超。
    # 依正負過濾，避免「movers 不足時」把淨買方塞進賣超榜（反之亦然）；賣超數字取絕對值顯示。
    _k = "foreign_net_buy_5d_lots"
    buy = [it for it in movers.get("top_foreign_buy_5d", []) if (it.get(_k) or 0) > 0][:3]
    sell = [it for it in movers.get("top_foreign_sell_5d", []) if (it.get(_k) or 0) < 0][:3]
    if buy:
        parts.append("**外資5日買超**\n"
                     + "\n".join(f"- {it['name']}：{it[_k]:,} 張" for it in buy))
    if sell:
        parts.append("**外資5日賣超**\n"
                     + "\n".join(f"- {it['name']}：{abs(it[_k]):,} 張" for it in sell))

    # 候選 / 要注意
    watch = report.get("tw_watchlist", [])
    caution = report.get("tw_caution", [])
    if watch:
        parts.append("**候選觀察（正向）**\n"
                     + "\n".join(f"- {w['symbol']} {w['name']}" for w in watch))
    if caution:
        parts.append("**要注意（負向）**\n"
                     + "\n".join(f"- {w['symbol']} {w['name']}" for w in caution))

    # 策略回測校準（依過去預估準確度自動修正；有資料才附）
    bt = report.get("backtest_summary") or {}
    if bt.get("sample_n"):
        ph = bt.get("primary_horizon", 5)
        wl = ((bt.get("metrics") or {}).get("watchlist") or {}).get(str(ph)) or {}
        if wl.get("n"):
            seg = [f"偏多近{ph}日方向準確率 {wl['direction_accuracy']*100:.0f}%"]
            if wl.get("avg_forward_return_pct") is not None:
                seg.append(f"平均報酬 {_pct(wl['avg_forward_return_pct'])}")
            parts.append(f"🎯 策略回測（樣本{bt['sample_n']}檔）：" + "、".join(seg))

    parts.append(f"\n📄 完整報告：{_report_url(report.get('report_id', ''))}")
    gr = report.get("guardrail") or {}
    if gr:
        status = "✅ 通過" if gr.get("passed") and not gr.get("warning_count") else f"⚠️ {gr.get('error_count',0)}攔/{gr.get('warning_count',0)}警"
        parts.append(f"🛡️ Guardrail：{status}")
    cost = report.get("cost") or {}
    if cost:
        parts.append(
            f"💰 {cost.get('month','本月')} AI 花費 NT${cost.get('month_total_twd',0):.2f}"
            f" / NT${cost.get('monthly_limit_twd',0):.0f}"
            f"（今日 NT${cost.get('day_total_twd',0):.2f}）"
        )
    parts.append("\n_此結果僅供家庭內部市場研究使用，不構成投資建議。_")

    text = "\n\n".join(parts)
    return text[:1990]  # Discord 2000 字上限留餘裕
