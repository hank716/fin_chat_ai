"""持股追蹤提醒：每天晨報後，把使用者自己的持股狀況推到他的**私人** Discord 頻道。

晨報只追當日 movers，使用者重倉的股票一旦不在排行裡就從晨報消失（2026-11 臻鼎-KY 即是）。
這裡補上「我的持股」視角：價格、籌碼、距損平與距自訂價位的距離，收盤穿越價位當天加 🔔。

設計約束：
- **隱私**：持股與損平價只送私人頻道。daily-report 頻道兩人共看、報告網頁經 Cloudflare 公開，
  所以這裡的內容**不得**寫進晨報 markdown / 網頁 / Discord 摘要 / 報告 JSON。
- **零 LLM 成本**：純程式產生，不經任何模型。
- **不得炸晨報**：任何錯誤都只記 log。

設定檔 `<LOCAL_STORAGE_PATH>/holdings.json`（主機 `./storage/holdings.json`，已在 .gitignore）：
    {"hank": [{"symbol": "4958", "breakeven": 445,
               "alert_below": [368, 357], "alert_above": [445, 490]}]}
user key 對應 `settings.discord_<user>_chat_channel_id`。範本見 configs/holdings.example.json。

CLI：`python -m reports.holdings_alert --dry-run`（只印）／無參數＝實送。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from config import settings

logger = logging.getLogger("ai-market-backend.holdings")

MAX_MESSAGE_CHARS = 1990   # Discord 2000 字上限留餘裕
FOOTER = "僅供參考，非投資建議；價位提醒以收盤價判定。"


class Holding(BaseModel):
    symbol: str
    breakeven: float | None = None
    alert_below: list[float] = Field(default_factory=list)
    alert_above: list[float] = Field(default_factory=list)


def holdings_path() -> Path:
    return Path(settings.local_storage_path) / "holdings.json"


def load_holdings(path: Path | None = None) -> dict[str, list[Holding]]:
    """讀設定檔。不存在＝沒設定（回空）；格式錯誤記 warning 回空——不能讓晨報因此失敗。"""
    p = path or holdings_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("最外層必須是 {user: [...]} 物件")
        return {
            str(user).strip().lower(): [Holding.model_validate(h) for h in (items or [])]
            for user, items in raw.items()
            if not str(user).startswith("_")      # "_說明" 之類的註解鍵
        }
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning("持股設定檔 %s 格式錯誤，略過持股提醒：%s", p, exc)
        return {}


def _pct(a: float, b: float) -> float:
    """a 相對 b 的百分比差。"""
    return round((a - b) / b * 100, 2)


def crossings(prev: float | None, close: float, below: list[float],
              above: list[float]) -> list[str]:
    """今天收盤穿越了哪些價位。

    向下：前日在價位之上、今日收在價位或以下；向上反之。前日已在同一側不重複觸發——
    否則跌破後每天都會再響一次，提醒就失去意義。
    """
    if prev is None:
        return []
    hits = [f"跌破 {_fmt(level)}" for level in sorted(below, reverse=True)
            if prev > level >= close]
    hits += [f"站上 {_fmt(level)}" for level in sorted(above) if prev < level <= close]
    return hits


def _fmt(v: float) -> str:
    return f"{v:g}"


def _prev_close(symbol: str) -> float | None:
    """前一交易日收盤（**未還原價**，與看盤軟體一致；使用者的價位也是照看盤價設的）。"""
    from storage import local_store
    df = local_store.read_prices(symbol, "TW")
    if df is None or len(df) < 2:
        return None
    df = df.sort_values("trade_date")
    return float(df["close"].iloc[-2])


def _fundamentals(symbol: str) -> dict[str, Any] | None:
    from data_sources.finmind_loader import FinMindBackoff
    from processor.fundamentals import build_fundamentals
    try:
        return build_fundamentals(symbol)
    except FinMindBackoff as exc:
        logger.warning("持股 %s 基本面因 FinMind 退避略過：%s", symbol, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("持股 %s 基本面抓取失敗：%s", symbol, exc)
    return None


def holding_status(h: Holding) -> dict[str, Any] | None:
    """組出單檔狀態。查無價格資料回 None（訊息裡會標「查無資料」）。"""
    from processor import tw_features
    try:
        block = tw_features.build_adhoc_symbol(h.symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("持股 %s 資料讀取失敗：%s", h.symbol, exc)
        return None
    if not block or block.get("close") is None:
        return None
    close = float(block["close"])
    prev = _prev_close(h.symbol)
    return {
        "symbol": h.symbol,
        "block": block,
        "close": close,
        "prev_close": prev,
        "vs_breakeven_pct": _pct(close, h.breakeven) if h.breakeven else None,
        "below": [(lv, _pct(lv, close)) for lv in sorted(h.alert_below, reverse=True)],
        "above": [(lv, _pct(lv, close)) for lv in sorted(h.alert_above)],
        "crossed": crossings(prev, close, h.alert_below, h.alert_above),
        "fundamentals": _fundamentals(h.symbol),
        "breakeven": h.breakeven,
    }


def _signed(v: float | None, suffix: str = "%") -> str:
    return "—" if v is None else f"{v:+.1f}{suffix}"


def _streak(label: str, n: int | None) -> str:
    if not n:
        return f"{label} 無連續"
    return f"{label} 連{'買' if n > 0 else '賣'}{abs(n)}日"


def render_holding(h: Holding, s: dict[str, Any] | None) -> str:
    if s is None:
        return f"📌 {h.symbol}　查無行情資料（代號錯誤或資料源暫時失敗）"
    b = s["block"]
    head = f"📌 {h.symbol} {b.get('name', '')}　{s['close']:g}（{_signed(b.get('return_1d_pct'))}）"
    if s["breakeven"]:
        head += f"　距損平 {_fmt(s['breakeven'])}：{_signed(s['vs_breakeven_pct'])}"
    lines = [
        head,
        f"5日 {_signed(b.get('return_5d_pct'))}｜20日 {_signed(b.get('return_20d_pct'))}"
        f"｜相對大盤20日 {_signed(b.get('vs_index_20d_pct'), 'pp')}",
    ]
    chip = [_streak("外資", b.get("foreign_net_streak"))]
    if b.get("foreign_net_buy_5d_lots") is not None:
        chip[0] += f"、5日 {b['foreign_net_buy_5d_lots']:+,}張"
    chip.append(_streak("投信", b.get("trust_net_streak")))
    if b.get("margin_chg_5d_lots") is not None:
        chip.append(f"融資5日 {b['margin_chg_5d_lots']:+,}張")
    lines.append("｜".join(chip))
    levels = []
    if s["below"]:
        levels.append("下方 " + "・".join(f"{_fmt(lv)}（{_signed(d)}）" for lv, d in s["below"]))
    if s["above"]:
        levels.append("上方 " + "・".join(f"{_fmt(lv)}（{_signed(d)}）" for lv, d in s["above"]))
    if levels:
        lines.append("｜".join(levels))
    fu = s.get("fundamentals") or {}
    if fu.get("revenue_100m") is not None:
        month = str(fu.get("revenue_month") or "")
        month_label = f"{int(month[5:7])}月" if len(month) >= 7 and month[5:7].isdigit() else "最新月"
        lines.append(f"{month_label}營收 {fu['revenue_100m']:g}億 YoY {_signed(fu.get('revenue_yoy_pct'))}")
    for hit in s["crossed"]:
        lines.append(f"🔔 今日收盤{hit}")
    return "\n".join(lines)


def render_message(holdings: list[Holding], statuses: list[dict[str, Any] | None],
                   as_of: str | None = None) -> str:
    title = "📊 我的持股" + (f"（資料日 {as_of}）" if as_of else "")
    blocks = [render_holding(h, s) for h, s in zip(holdings, statuses)]
    text = "\n\n".join([title, *blocks, FOOTER])
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 20].rstrip() + "\n…（已截斷）"
    return text


def _channel_of(user: str) -> str:
    return str(getattr(settings, f"discord_{user}_chat_channel_id", "") or "").strip()


def build_messages(holdings: dict[str, list[Holding]] | None = None) -> dict[str, str]:
    """{user: 訊息文字}。沒有持股的 user 不產生訊息。"""
    holdings = load_holdings() if holdings is None else holdings
    out: dict[str, str] = {}
    for user, items in holdings.items():
        if not items:
            continue
        statuses = [holding_status(h) for h in items]
        as_of = next((s["block"].get("as_of") for s in statuses if s), None)
        out[user] = render_message(items, statuses, as_of)
    return out


def send_holdings_alerts(holdings: dict[str, list[Holding]] | None = None) -> dict[str, bool]:
    """逐 user 推到其私人頻道。頻道沒設定就略過（絕不退回共用的 daily-report 頻道）。"""
    from notify.discord import send_message
    results: dict[str, bool] = {}
    for user, text in build_messages(holdings).items():
        channel = _channel_of(user)
        if not channel:
            logger.warning("持股提醒：%s 沒有設定私人頻道 discord_%s_chat_channel_id，略過", user, user)
            results[user] = False
            continue
        results[user] = send_message(channel, text)
    if results:
        logger.info("持股提醒推送結果：%s", results)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="持股追蹤提醒")
    parser.add_argument("--dry-run", action="store_true", help="只印出訊息，不送 Discord")
    args = parser.parse_args(argv)
    holdings = load_holdings()
    if not holdings:
        print(f"沒有持股設定（{holdings_path()} 不存在或為空）")
        return 1
    if args.dry_run:
        for user, text in build_messages(holdings).items():
            print(f"=== {user} → 頻道 {'已設定' if _channel_of(user) else '未設定'} ===\n{text}\n")
        return 0
    print(send_holdings_alerts(holdings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
