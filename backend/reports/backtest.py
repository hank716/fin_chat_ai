"""晨報預估回測引擎（策略自動修正迴圈第 1 環，純本地、零 LLM 成本）。

對「過去晨報給過的預估」做事後評分：每份 storage/reports/morning_*.json 裡的
tw_watchlist（偏多）/ tw_caution（偏空）每檔都帶 target_price/stop_loss/signals，
這裡用已落地的 parquet 行情（local_store.read_prices）回測它們後來準不準：

  進場價 = features.tw.stocks[sym].close（晨報資料日收盤；缺值回退 parquet）
  未來窗 = 資料日之後 N 個交易日的 OHLC（嚴格 trade_date > as_of，杜絕未來洩漏）

每檔對「5 日 / 20 日」雙窗各算一份成績：是否觸目標價、是否觸止損、方向對不對、
未來報酬、最大有利/不利波幅（MFE/MAE）、相對大盤超額報酬。聚合成 scorecard 落地
storage/backtests/{report_id}.json。run_due_evaluations 冪等、只評「已到期」的窗。

featurize() 把個股當日特徵抽成數值向量，連同標籤一起存進 scorecard，供
strategy_calibration 訓練本地 edge 模型時直接讀（免再載大份 report.json）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from config import settings
from storage import local_store

logger = logging.getLogger("ai-market-backend.backtest")

TW_MARKET = "tw"
INDEX_SYMBOL = "TWII"
REPORTS_DIR = Path(settings.local_storage_path) / "reports"
BACKTEST_DIR = Path(settings.local_storage_path) / "backtests"

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def horizons() -> list[int]:
    """設定的評估窗（交易日），由小到大去重。"""
    out: list[int] = []
    for part in str(settings.backtest_horizons).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if n > 0:
                out.append(n)
        except ValueError:
            continue
    return sorted(set(out)) or [5, 20]


def _parse_price(s: Any) -> float | None:
    """從 target_price/stop_loss 字串取第一個數字（容忍 "480.0"、"約 480"、"480~500"）。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = _NUM_RE.search(str(s))
    if not m:
        return None
    try:
        v = float(m.group())
        return v if v > 0 else None
    except ValueError:
        return None


def _stock_entry(report: dict[str, Any], sym: str) -> dict[str, Any]:
    return ((report.get("features", {}) or {}).get("tw", {}) or {}).get("stocks", {}).get(sym, {}) or {}


def _entry_close(report: dict[str, Any], sym: str, as_of: pd.Timestamp) -> float | None:
    """進場價：優先用晨報 features 內的當日收盤；缺則回退 parquet 中 <= as_of 的最後收盤。"""
    entry = _stock_entry(report, sym)
    c = entry.get("close")
    if isinstance(c, (int, float)) and c > 0:
        return float(c)
    df = local_store.read_prices(sym, TW_MARKET)
    if df.empty:
        return None
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    past = df[df["trade_date"] <= as_of].sort_values("trade_date")
    if past.empty:
        return None
    val = past.iloc[-1]["close"]
    return float(val) if pd.notna(val) else None


def _forward_window(sym: str, as_of: pd.Timestamp, n: int) -> pd.DataFrame:
    """資料日之後的前 n 個交易日 OHLC（trade_date > as_of，嚴防未來洩漏）。"""
    df = local_store.read_prices(sym, TW_MARKET)
    if df.empty:
        return df
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    fwd = df[df["trade_date"] > as_of].sort_values("trade_date").head(n)
    return fwd.reset_index(drop=True)


def _index_forward_return(as_of: pd.Timestamp, n: int) -> float | None:
    fwd = _forward_window(INDEX_SYMBOL, as_of, n)
    if fwd.empty:
        return None
    first_prev = local_store.read_prices(INDEX_SYMBOL, TW_MARKET)
    if first_prev.empty:
        return None
    first_prev = first_prev.copy()
    first_prev["trade_date"] = pd.to_datetime(first_prev["trade_date"])
    base = first_prev[first_prev["trade_date"] <= as_of].sort_values("trade_date")
    if base.empty:
        return None
    entry = float(base.iloc[-1]["close"])
    last = float(fwd.iloc[-1]["close"])
    if entry <= 0:
        return None
    return round((last - entry) / entry * 100, 2)


_SIGNAL_KEYS = {
    "外資連買": lambda s: "外資" in s and "連買" in s,
    "外資買超": lambda s: "外資" in s and "買超" in s,
    "投信連買": lambda s: "投信" in s and "連買" in s,
    "投信買超": lambda s: "投信" in s and "買超" in s,
    "站上均線": lambda s: "站上" in s or "MA20" in s.upper() and "跌破" not in s,
    "外資連賣": lambda s: "外資" in s and "連賣" in s,
    "外資賣超": lambda s: "外資" in s and "賣超" in s,
    "投信賣超": lambda s: ("投信" in s and "賣超" in s) or ("投信" in s and "連賣" in s),
    "跌破均線": lambda s: "跌破" in s,
    "資券比偏高": lambda s: "資券比" in s,
    "相對大盤弱": lambda s: "相對大盤" in s and "-" in s,
}


def signal_tags(signals: list[str]) -> list[str]:
    """把自由文字 signals 對應到固定關鍵字桶（供訊號效力統計）。"""
    tags: list[str] = []
    for raw in signals or []:
        s = str(raw)
        for tag, match in _SIGNAL_KEYS.items():
            try:
                if match(s) and tag not in tags:
                    tags.append(tag)
            except Exception:  # noqa: BLE001
                continue
    return tags


# edge 模型用的數值特徵欄位（順序固定；缺值留 None，HistGBT 原生吃 NaN）
FEATURE_COLUMNS = [
    "return_1d_pct", "return_5d_pct", "return_20d_pct", "volatility_20d_pct",
    "above_ma20", "above_ma60", "vs_index_20d_pct",
    "foreign_net_buy_5d_lots", "foreign_net_streak", "trust_net_streak",
    "margin_chg_5d_lots", "short_margin_ratio_pct", "side_bull",
]


def _bool_to_num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return None


def featurize(stock_entry: dict[str, Any], side: str) -> dict[str, float | None]:
    """個股當日 features → edge 模型輸入向量（FEATURE_COLUMNS 順序）。"""
    e = stock_entry or {}
    return {
        "return_1d_pct": _bool_to_num(e.get("return_1d_pct")),
        "return_5d_pct": _bool_to_num(e.get("return_5d_pct")),
        "return_20d_pct": _bool_to_num(e.get("return_20d_pct")),
        "volatility_20d_pct": _bool_to_num(e.get("volatility_20d_pct")),
        "above_ma20": _bool_to_num(e.get("above_ma20")),
        "above_ma60": _bool_to_num(e.get("above_ma60")),
        "vs_index_20d_pct": _bool_to_num(e.get("vs_index_20d_pct")),
        "foreign_net_buy_5d_lots": _bool_to_num(e.get("foreign_net_buy_5d_lots")),
        "foreign_net_streak": _bool_to_num(e.get("foreign_net_streak")),
        "trust_net_streak": _bool_to_num(e.get("trust_net_streak")),
        "margin_chg_5d_lots": _bool_to_num(e.get("margin_chg_5d_lots")),
        "short_margin_ratio_pct": _bool_to_num(e.get("short_margin_ratio_pct")),
        "side_bull": 1.0 if side == "watchlist" else 0.0,
    }


def evaluate_item(
    side: str, entry: float, target: float | None, stop: float | None, fwd: pd.DataFrame,
    index_ret: float | None,
) -> dict[str, Any]:
    """單檔單一窗的回測結果。bullish(watchlist)：目標在上、止損在下；caution：方向對=下跌。"""
    if fwd.empty or not entry or entry <= 0:
        return {"outcome": "no_data", "n_days": 0}

    highs = fwd["high"].astype(float).tolist()
    lows = fwd["low"].astype(float).tolist()
    closes = fwd["close"].astype(float).tolist()
    final_close = closes[-1]
    forward_return_pct = round((final_close - entry) / entry * 100, 2)
    mfe_pct = round((max(highs) - entry) / entry * 100, 2)
    mae_pct = round((min(lows) - entry) / entry * 100, 2)

    bull = side == "watchlist"
    direction_correct = forward_return_pct > 0 if bull else forward_return_pct < 0

    # 觸價判定：逐日找最早觸發；同日同時觸目標與止損 → 保守記 stop。
    outcome = "open"
    days_to_event: int | None = None
    for i in range(len(fwd)):
        hit_target = target is not None and (
            (bull and highs[i] >= target) or (not bull and lows[i] <= target)
        )
        hit_stop = stop is not None and (
            (bull and lows[i] <= stop) or (not bull and highs[i] >= stop)
        )
        if hit_stop:
            outcome, days_to_event = "stop_hit", i + 1
            break
        if hit_target:
            outcome, days_to_event = "target_hit", i + 1
            break

    implied_target_pct = (
        round((target - entry) / entry * 100, 2) if target is not None else None
    )
    vs_index_pct = (
        round(forward_return_pct - index_ret, 2) if index_ret is not None else None
    )
    return {
        "outcome": outcome,
        "days_to_event": days_to_event,
        "n_days": len(fwd),
        "forward_return_pct": forward_return_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "implied_target_pct": implied_target_pct,
        "direction_correct": bool(direction_correct),
        "vs_index_pct": vs_index_pct,
    }


def _data_matured(as_of: pd.Timestamp, h: int) -> bool:
    """大盤在 as_of 之後是否已有 >= h 個交易日資料（該窗是否到期可評）。"""
    return len(_forward_window(INDEX_SYMBOL, as_of, h)) >= h


def evaluate_report(report: dict[str, Any], hs: list[int] | None = None) -> dict[str, Any]:
    """對一份晨報的 watchlist/caution 各檔 × 各窗回測，回 scorecard（含聚合與成熟旗標）。"""
    hs = hs or horizons()
    feats = report.get("features", {}) or {}
    as_of_str = feats.get("as_of") or report.get("data_as_of")
    as_of = pd.to_datetime(as_of_str) if as_of_str else None
    matured = {h: bool(as_of is not None and _data_matured(as_of, h)) for h in hs}
    index_ret = {h: (_index_forward_return(as_of, h) if as_of is not None else None) for h in hs}

    items: list[dict[str, Any]] = []
    for side in ("watchlist", "caution"):
        for it in report.get(f"tw_{side}", []) or []:
            sym = it.get("symbol")
            if not sym or as_of is None:
                continue
            entry = _entry_close(report, sym, as_of)
            target = _parse_price(it.get("target_price"))
            stop = _parse_price(it.get("stop_loss"))
            stock_entry = _stock_entry(report, sym)
            per_h: dict[str, Any] = {}
            for h in hs:
                if not matured[h]:
                    per_h[str(h)] = {"outcome": "pending", "n_days": 0}
                    continue
                fwd = _forward_window(sym, as_of, h)
                per_h[str(h)] = evaluate_item(side, entry or 0.0, target, stop, fwd, index_ret[h])
            items.append({
                "symbol": sym,
                "name": it.get("name") or stock_entry.get("name") or sym,
                "sector": it.get("sector") or stock_entry.get("sector"),
                "side": side,
                "signals": it.get("signals", []),
                "signal_tags": signal_tags(it.get("signals", [])),
                "entry": entry,
                "target": target,
                "stop": stop,
                "features": featurize(stock_entry, side),
                "horizons": per_h,
            })

    return {
        "report_id": report.get("report_id"),
        "report_date": report.get("report_date"),
        "as_of": as_of_str,
        "generated_at": report.get("generated_at"),
        "horizons": hs,
        "matured": {str(h): matured[h] for h in hs},
        # 該晨報是否曾注入策略校準（供成效量測比較「校準前/後」兩個世代，免再載 report.json）
        "calibration_injected": bool(report.get("calibration_injected")),
        "items": items,
        "aggregates": _aggregate(items, hs),
    }


def _aggregate(items: list[dict[str, Any]], hs: list[int]) -> dict[str, Any]:
    """依 side × horizon 聚合命中率/方向準確率/平均報酬等。只計入已評（非 pending/no_data）的檔。"""
    agg: dict[str, Any] = {}
    for side in ("watchlist", "caution"):
        agg[side] = {}
        side_items = [it for it in items if it["side"] == side]
        for h in hs:
            rows = [
                it["horizons"][str(h)] for it in side_items
                if it["horizons"].get(str(h), {}).get("outcome") not in (None, "pending", "no_data")
            ]
            n = len(rows)
            if n == 0:
                agg[side][str(h)] = {"n": 0}
                continue
            n_target = sum(1 for it in side_items if it.get("target") is not None
                           and it["horizons"][str(h)].get("outcome") not in (None, "pending", "no_data"))
            n_stop = sum(1 for it in side_items if it.get("stop") is not None
                         and it["horizons"][str(h)].get("outcome") not in (None, "pending", "no_data"))
            target_hits = sum(1 for r in rows if r.get("outcome") == "target_hit")
            stop_hits = sum(1 for r in rows if r.get("outcome") == "stop_hit")
            dir_ok = sum(1 for r in rows if r.get("direction_correct"))
            fwd_rets = [r["forward_return_pct"] for r in rows if r.get("forward_return_pct") is not None]
            mfes = [r["mfe_pct"] for r in rows if r.get("mfe_pct") is not None]
            implied = [r["implied_target_pct"] for r in rows if r.get("implied_target_pct") is not None]
            avg_mfe = round(sum(mfes) / len(mfes), 2) if mfes else None
            avg_implied = round(sum(implied) / len(implied), 2) if implied else None
            # 目標價樂觀度：預估目標漲幅 / 實際最大有利波幅。>1 代表目標訂太高（偏樂觀）。
            optimism = (
                round(avg_implied / avg_mfe, 2)
                if avg_implied is not None and avg_mfe not in (None, 0) and avg_mfe > 0 else None
            )
            agg[side][str(h)] = {
                "n": n,
                "target_hit_rate": round(target_hits / n_target, 3) if n_target else None,
                "stop_hit_rate": round(stop_hits / n_stop, 3) if n_stop else None,
                "direction_accuracy": round(dir_ok / n, 3),
                "avg_forward_return_pct": round(sum(fwd_rets) / len(fwd_rets), 2) if fwd_rets else None,
                "avg_mfe_pct": avg_mfe,
                "avg_implied_target_pct": avg_implied,
                "target_optimism": optimism,
            }
    return agg


def _scorecard_path(report_id: str) -> Path:
    return BACKTEST_DIR / f"{report_id}.json"


def _fully_matured(scorecard: dict[str, Any]) -> bool:
    return all(scorecard.get("matured", {}).values()) if scorecard.get("matured") else False


def run_due_evaluations(hs: list[int] | None = None) -> dict[str, Any]:
    """掃所有晨報，回測「新到期」的窗並落地 scorecard。冪等：已全到期的報告跳過。

    純讀本機 parquet + report.json，不打任何外部 API、不產生 LLM 花費。
    """
    hs = hs or horizons()
    if not REPORTS_DIR.exists():
        return {"evaluated": 0, "skipped": 0, "reason": "no reports dir"}
    # 本機回測/模型訓練是 CPU 工作（非對外流量）：記一次活動，讓「待機建議」在運算時不建議睡。
    # 放在這裡可同時覆蓋晨報迴圈與 /brief/backtest 端點（兩者都先呼叫 run_due_evaluations）。
    try:
        from activity import monitor
        monitor.mark("compute")
    except Exception:  # noqa: BLE001 — 埋點絕不可拖垮回測
        pass
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    evaluated = skipped = errors = 0
    for rp in sorted(REPORTS_DIR.glob("morning_*.json")):
        report_id = rp.stem
        sc_path = _scorecard_path(report_id)
        if sc_path.exists():
            try:
                existing = json.loads(sc_path.read_text(encoding="utf-8"))
                if _fully_matured(existing):
                    skipped += 1
                    continue
            except Exception:  # noqa: BLE001 — 壞 scorecard 直接重算
                pass
        try:
            report = json.loads(rp.read_text(encoding="utf-8"))
            scorecard = evaluate_report(report, hs)
            sc_path.write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")
            evaluated += 1
        except Exception as exc:  # noqa: BLE001 — 單份失敗不阻斷其他
            logger.warning("回測 %s 失敗: %s", report_id, exc)
            errors += 1
    logger.info("回測完成：評估 %d 份、跳過(已到期) %d 份、失敗 %d 份", evaluated, skipped, errors)
    return {"evaluated": evaluated, "skipped": skipped, "errors": errors, "horizons": hs}


def load_scorecards(limit: int | None = None) -> list[dict[str, Any]]:
    """讀回 scorecard（新到舊）。"""
    if not BACKTEST_DIR.exists():
        return []
    files = sorted(BACKTEST_DIR.glob("morning_*.json"), reverse=True)
    if limit:
        files = files[:limit]
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return out
