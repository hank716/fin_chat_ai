"""策略自動修正（回測迴圈第 2 環）：把回測 scorecard 彙整成校準，回灌晨報並訓練本地模型。

兩層自動修正：
  ①文字校準回灌 prompt（第 1 天就有效）：把命中率/止損率/目標價樂觀度/訊號效力濃縮成
    一段繁中「校準提示」，由 morning_brief 注入晨報 prompt，讓 Gemini 下次自我修正。
  ②本地 ML edge 模型（資料累積後生效）：用回測標籤訓練 HistGradientBoosting，替當日候選
    打「成功機率」供重排；特徵重要度也回灌校準文字。樣本不足時自動跳過、退回純文字校準。

全程讀本機 scorecard，CPU 數秒可完成，不打外部 API、零 LLM 花費（對齊使用者「以時間換
運算」且本機 GTX 1060 不適合跑 LLM 的硬體現實——策略大腦用表格式 ML 才是正解）。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from reports import backtest
from reports.backtest import FEATURE_COLUMNS, featurize

logger = logging.getLogger("ai-market-backend.strategy_calibration")

STRATEGY_DIR = Path(settings.local_storage_path) / "strategy"
CALIBRATION_PATH = STRATEGY_DIR / "calibration.json"
EDGE_MODEL_PATH = STRATEGY_DIR / "edge_model.pkl"
EDGE_META_PATH = STRATEGY_DIR / "edge_model_meta.json"
EVAL_PATH = STRATEGY_DIR / "evaluation.json"

# 成效量測：足以下定論所需的最少樣本與交易日數（跨多種行情才不被單一 regime 帶偏）
EVAL_MIN_DATES = 10

# 文字校準的最低樣本門檻（低於此不注入，避免雜訊誤導模型）
MIN_TEXT_SAMPLES = 12
# edge 模型只有在保留樣本上展現預測力（AUC > 此值）才拿來重排候選，否則不動——
# 不讓「沒 edge 的模型」誤導選股（例：5 日窗 AUC 0.44 < 0.5 等於沒用，就該略過）。
EDGE_MIN_AUC = 0.52
_VALID = lambda r: r and r.get("outcome") not in (None, "pending", "no_data")  # noqa: E731


def _primary_h() -> int:
    return backtest.horizons()[0]


# ───────────────────────── 彙整 / 校準 ─────────────────────────

def _signal_ranking(scorecards: list[dict[str, Any]], h: int) -> list[dict[str, Any]]:
    """各訊號桶的效力：以「方向校正後報酬」(偏多取正、偏空取負) 排名。"""
    buckets: dict[str, list[float]] = {}
    wins: dict[str, int] = {}
    for sc in scorecards:
        for it in sc.get("items", []):
            r = it.get("horizons", {}).get(str(h))
            if not _VALID(r) or r.get("forward_return_pct") is None:
                continue
            sign = 1.0 if it.get("side") == "watchlist" else -1.0
            edge = sign * r["forward_return_pct"]
            for tag in it.get("signal_tags", []):
                buckets.setdefault(tag, []).append(edge)
                wins[tag] = wins.get(tag, 0) + (1 if r.get("direction_correct") else 0)
    out = []
    for tag, vals in buckets.items():
        if not vals:
            continue
        out.append({
            "tag": tag,
            "n": len(vals),
            "avg_edge_return_pct": round(sum(vals) / len(vals), 2),
            "win_rate": round(wins.get(tag, 0) / len(vals), 3),
        })
    out.sort(key=lambda x: x["avg_edge_return_pct"], reverse=True)
    return out


def rebuild(lookback: int | None = None) -> dict[str, Any]:
    """彙整最近 N 份已到期 scorecard → calibration.json，回傳 summary。"""
    lookback = lookback or settings.backtest_calibration_lookback
    hs = backtest.horizons()
    scorecards = backtest.load_scorecards(limit=lookback)
    all_items = [it for sc in scorecards for it in sc.get("items", [])]
    metrics = backtest._aggregate(all_items, hs)  # 跨報告 pool 聚合（同 side×horizon 公式）

    ph = _primary_h()
    ranking = _signal_ranking(scorecards, ph)
    # 樣本數（偏多主窗）作為校準是否成熟的依據
    sample_n = metrics.get("watchlist", {}).get(str(ph), {}).get("n", 0)

    edge_meta = _load_json(EDGE_META_PATH) or {}
    summary = {
        "generated_at": datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds"),
        "lookback_reports": len(scorecards),
        "primary_horizon": ph,
        "horizons": hs,
        "sample_n": sample_n,
        "metrics": metrics,
        "signal_ranking": ranking,
        "edge_model": edge_meta,
    }
    summary["calibration_text"] = _compose_text(summary)
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    CALIBRATION_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def latest_summary() -> dict[str, Any]:
    """讀回最近一次彙整（給首頁/報告/Discord 顯示；無檔回空 dict）。"""
    return _load_json(CALIBRATION_PATH) or {}


# ───────────────────────── 成效量測 harness ─────────────────────────
# 把「會不會準」變成可看的數字：最誠實的指標——選股有沒有贏大盤——不需任何 ML，
# 直接用 scorecard 已存的 vs_index_pct（相對大盤超額）算出來。

def _excess_stats(scorecards: list[dict[str, Any]], side: str, h: int) -> dict[str, Any]:
    """某 side×窗 的成效：方向準確率、平均報酬、**平均超額（贏大盤幅度）**、贏過大盤比率。"""
    rows = []
    for sc in scorecards:
        for it in sc.get("items", []):
            if it.get("side") != side:
                continue
            r = it.get("horizons", {}).get(str(h))
            if _VALID(r):
                rows.append(r)
    n = len(rows)
    if n == 0:
        return {"n": 0}
    fwd = [r["forward_return_pct"] for r in rows if r.get("forward_return_pct") is not None]
    exc = [r["vs_index_pct"] for r in rows if r.get("vs_index_pct") is not None]
    dir_ok = sum(1 for r in rows if r.get("direction_correct"))
    beat = sum(1 for v in exc if v > 0)
    return {
        "n": n,
        "direction_accuracy": round(dir_ok / n, 3),
        "avg_forward_return_pct": round(sum(fwd) / len(fwd), 2) if fwd else None,
        "avg_excess_vs_market_pct": round(sum(exc) / len(exc), 2) if exc else None,
        "beat_market_rate": round(beat / len(exc), 3) if exc else None,
    }


def _eval_verdict(sufficient: bool, n_dates: int, span: int,
                  wl: dict[str, Any], cau: dict[str, Any], edge: dict[str, Any]) -> str:
    """誠實白話判讀：先講資料夠不夠下定論，再講目前數字顯示什麼。"""
    parts: list[str] = []
    if not sufficient:
        parts.append(
            f"⚠️ 樣本仍不足以下定論（偏多 {wl.get('n',0)} 檔、{n_dates} 個交易日、跨 {span} 天；"
            f"需 ≥{settings.edge_model_min_samples} 檔且涵蓋多種行情）。下列數字僅供參考、會隨資料變動。"
        )
    else:
        parts.append(f"資料量已具參考性（偏多 {wl.get('n',0)} 檔、{n_dates} 個交易日、跨 {span} 天）。")

    ex = wl.get("avg_excess_vs_market_pct")
    br = wl.get("beat_market_rate")
    if ex is not None and br is not None:
        verb = "贏" if ex > 0 else "輸"
        parts.append(f"偏多選股平均{verb}大盤 {abs(ex):.1f}%、贏過大盤比率 {br*100:.0f}%"
                     + ("（有超額、選股有加值）。" if ex > 0 else "（落後大盤，選股目前未加值）。"))
    if cau.get("direction_accuracy") is not None:
        parts.append(f"避雷側方向準確率 {cau['direction_accuracy']*100:.0f}%。")
    if edge.get("trained") and edge.get("holdout_auc") is not None:
        auc = edge["holdout_auc"]
        parts.append(f"edge 模型 OOS AUC {auc}（>0.5 才有預測力，{'有' if auc > 0.55 else '尚無明顯'}edge）。")
    else:
        parts.append(f"edge 模型尚未訓練（樣本 {edge.get('n_samples', 0)}/{settings.edge_model_min_samples}）。")
    return "".join(parts)


def evaluate_effectiveness(lookback: int | None = None) -> dict[str, Any]:
    """量測策略成效並落地 evaluation.json。純讀 scorecard（excess 指標零 ML），回 summary。"""
    lookback = lookback or settings.backtest_calibration_lookback
    hs = backtest.horizons()
    ph = hs[0]
    scorecards = backtest.load_scorecards(limit=lookback)
    dates = sorted({sc.get("as_of") for sc in scorecards if sc.get("as_of")})
    span = 0
    if len(dates) >= 2:
        try:
            span = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days
        except ValueError:
            span = 0

    wl = _excess_stats(scorecards, "watchlist", ph)
    cau = _excess_stats(scorecards, "caution", ph)
    sufficient = wl.get("n", 0) >= settings.edge_model_min_samples and len(dates) >= EVAL_MIN_DATES

    # 校準前/後世代比較（confounded by regime，僅供長期追蹤，附警語）
    def _era(flag: bool) -> dict[str, Any]:
        return _excess_stats([sc for sc in scorecards if bool(sc.get("calibration_injected")) == flag],
                             "watchlist", ph)
    eras = {"with_calibration": _era(True), "without_calibration": _era(False)}

    edge = _load_json(EDGE_META_PATH) or {}
    out = {
        "generated_at": datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds"),
        "primary_horizon": ph,
        "data": {
            "watchlist_n": wl.get("n", 0), "caution_n": cau.get("n", 0),
            "distinct_dates": len(dates), "date_span_days": span,
            "date_range": [dates[0], dates[-1]] if dates else [],
            "sufficient": sufficient, "min_samples_needed": settings.edge_model_min_samples,
        },
        "watchlist": wl,
        "caution": cau,
        "calibration_eras": eras,
        "calibration_eras_note": "校準前/後比較會被市場行情干擾（confounded），僅供長期趨勢參考、不可單獨下因果結論。",
        "edge_model": {k: edge.get(k) for k in ("trained", "n_samples", "holdout_accuracy", "holdout_auc")},
        "verdict": _eval_verdict(sufficient, len(dates), span, wl, cau, edge),
    }
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    EVAL_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def latest_evaluation() -> dict[str, Any]:
    """讀回最近一次成效量測（給首頁/端點顯示；無檔回空 dict）。"""
    return _load_json(EVAL_PATH) or {}


def _compose_text(summary: dict[str, Any]) -> str:
    """把 summary 濃縮成繁中校準提示（樣本不足回空字串）。"""
    if summary.get("sample_n", 0) < MIN_TEXT_SAMPLES:
        return ""
    ph = summary["primary_horizon"]
    m = summary.get("metrics", {})
    lines: list[str] = []
    wl = m.get("watchlist", {}).get(str(ph), {})
    if wl.get("n"):
        seg = [f"偏多清單近{ph}日方向準確率 {wl['direction_accuracy']*100:.0f}%"]
        if wl.get("target_hit_rate") is not None:
            seg.append(f"目標價命中 {wl['target_hit_rate']*100:.0f}%")
        if wl.get("stop_hit_rate") is not None:
            seg.append(f"觸止損 {wl['stop_hit_rate']*100:.0f}%")
        if wl.get("avg_forward_return_pct") is not None:
            seg.append(f"平均報酬 {wl['avg_forward_return_pct']:+.1f}%")
        lines.append("、".join(seg) + "。")
        opt = wl.get("target_optimism")
        if opt is not None and opt > 1.3:
            lines.append(f"⚠️目標價偏樂觀（預估漲幅約為實際最大波幅的 {opt:.1f} 倍）：請把目標價訂保守些、貼近實際壓力區。")
        elif opt is not None and opt < 0.7:
            lines.append("目標價略保守，可適度上調以反映實際波動。")
        if wl.get("stop_hit_rate") is not None and wl["stop_hit_rate"] > 0.4:
            lines.append("止損觸發偏頻繁：止損可略放寬或進場點更嚴格。")
    cau = m.get("caution", {}).get(str(ph), {})
    if cau.get("n") and cau.get("direction_accuracy") is not None:
        lines.append(f"偏空/要注意清單近{ph}日方向準確率 {cau['direction_accuracy']*100:.0f}%"
                     + (f"、平均報酬 {cau['avg_forward_return_pct']:+.1f}%。" if cau.get("avg_forward_return_pct") is not None else "。"))

    ranking = summary.get("signal_ranking", [])
    good = [r["tag"] for r in ranking if r["n"] >= 3][:3]
    bad = [r["tag"] for r in reversed(ranking) if r["n"] >= 3][:3]
    if good:
        lines.append("過往較有效的訊號：" + "、".join(good) + "（可優先採用）。")
    if bad and set(bad) != set(good):
        lines.append("過往較弱/易失準的訊號：" + "、".join(bad) + "（採用時請更謹慎或要求更強佐證）。")

    edge = summary.get("edge_model", {})
    if edge.get("top_features"):
        lines.append("本地模型顯示最關鍵的選股因子：" + "、".join(edge["top_features"][:4]) + "。")

    if not lines:
        return ""
    return ("【策略校準（系統依過去回測自動修正，僅供你調整選股傾向，數據仍以 features 為準）】\n"
            + "\n".join(f"- {ln}" for ln in lines))


def build_calibration_block() -> str:
    """供 morning_brief 注入晨報 prompt 的校準文字。停用或樣本不足回空字串。"""
    if not settings.enable_strategy_calibration:
        return ""
    try:
        summary = latest_summary()
        return summary.get("calibration_text", "") or ""
    except Exception as exc:  # noqa: BLE001 — 校準絕不可阻斷晨報
        logger.warning("讀取校準文字失敗: %s", exc)
        return ""


# ───────────────────────── 本地 edge 模型 ─────────────────────────

def _model_path(h: int) -> Path:
    """每個時間窗各自一顆模型（edge_model_5.pkl / edge_model_20.pkl）。"""
    return STRATEGY_DIR / f"edge_model_{h}.pkl"


def _live_samples(h: int) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    """從 scorecard 組某窗的『線上真選股』樣本：X=featurize、y=方向是否正確、date 供時間切分。"""
    X: list[dict[str, Any]] = []
    y: list[int] = []
    dates: list[str] = []
    for sc in backtest.load_scorecards():
        d = sc.get("as_of") or sc.get("report_date") or ""
        for it in sc.get("items", []):
            r = it.get("horizons", {}).get(str(h))
            if not _VALID(r) or r.get("direction_correct") is None:
                continue
            feat = it.get("features") or featurize({}, it.get("side", "watchlist"))
            X.append({c: feat.get(c) for c in FEATURE_COLUMNS})
            y.append(1 if r["direction_correct"] else 0)
            dates.append(d)
    return X, y, dates


def train_edge_model() -> dict[str, Any]:
    """訓練本地 edge 模型——**每個窗（5/20 日）各一顆**，吃『歷史回放選股集 + 線上真選股』。

    歷史集（reports.training_set）讓樣本一次到位、不必等數週；兩者合併後做時間序 walk-forward。
    回 combined meta：頂層＝主窗（相容首頁/判讀），另含 per_horizon 各窗明細。
    """
    if not settings.enable_edge_model:
        return {"trained": False, "reason": "disabled"}
    try:
        import joblib  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: PLC0415
        from sklearn.inspection import permutation_importance  # noqa: PLC0415
        from sklearn.metrics import accuracy_score, roc_auc_score  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — 套件未裝（尚未 rebuild 容器）→ 靜默跳過
        logger.warning("edge 模型套件未就緒，跳過訓練: %s", exc)
        return {"trained": False, "reason": f"import failed: {exc}"}

    from reports import training_set  # noqa: PLC0415

    minimum = settings.edge_model_min_samples

    def _dataset(h: int):
        """某窗的完整訓練集（歷史回放 + 線上）：回 (Xdf, y, dates)，按時間排序。"""
        frames = []
        hist = training_set.load_training_set(h)
        if not hist.empty:
            hd = hist[FEATURE_COLUMNS].astype(float).copy()
            hd["__y"] = hist["label"].astype(int).to_numpy()
            hd["__d"] = hist["as_of"].astype(str).to_numpy()
            frames.append(hd)
        Xl, yl, dl = _live_samples(h)
        if Xl:
            ld = pd.DataFrame(Xl, columns=FEATURE_COLUMNS).astype(float)
            ld["__y"] = yl
            ld["__d"] = dl
            frames.append(ld)
        if not frames:
            return None
        ds = pd.concat(frames, ignore_index=True).sort_values("__d").reset_index(drop=True)
        return ds

    def _train_one(h: int) -> dict[str, Any]:
        ds = _dataset(h)
        n = 0 if ds is None else len(ds)
        if ds is None or n < minimum or ds["__y"].nunique() < 2:
            reason = (f"樣本不足 {n}/{minimum}" if (ds is None or n < minimum)
                      else f"標籤僅單一類別（n={n}）")
            return {"trained": False, "reason": reason, "n_samples": n,
                    "min_samples": minimum, "horizon": h}
        X = ds[FEATURE_COLUMNS]
        yv = ds["__y"].to_numpy()
        split = max(1, int(n * 0.8))
        X_tr, X_te, y_tr, y_te = X.iloc[:split], X.iloc[split:], yv[:split], yv[split:]
        model = HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.05, max_depth=4, l2_regularization=1.0, random_state=42,
        )
        model.fit(X_tr, y_tr)
        acc = auc = None
        top: list[str] = []
        if len(y_te) >= 5 and len(set(y_te)) == 2:
            acc = round(float(accuracy_score(y_te, model.predict(X_te))), 3)
            auc = round(float(roc_auc_score(y_te, model.predict_proba(X_te)[:, 1])), 3)
            try:
                imp = permutation_importance(model, X_te, y_te, n_repeats=5, random_state=42)
                ranked = sorted(zip(FEATURE_COLUMNS, imp.importances_mean), key=lambda t: t[1], reverse=True)
                top = [c for c, v in ranked if v > 0][:5] or [ranked[0][0]]
            except Exception:  # noqa: BLE001
                top = []
        model.fit(X, yv)                      # 全資料重擬合上線
        STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": model, "columns": FEATURE_COLUMNS}, _model_path(h))
        return {"trained": True, "n_samples": n, "min_samples": minimum, "horizon": h,
                "holdout_accuracy": acc, "holdout_auc": auc, "top_features": top,
                "positive_rate": round(float(yv.mean()), 3)}

    hs = backtest.horizons()
    per = {h: _train_one(h) for h in hs}
    ph = _primary_h()
    primary = per.get(ph, {"trained": False, "n_samples": 0, "min_samples": minimum})
    meta = {
        **primary,
        "trained_at": datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds"),
        "primary_horizon": ph,
        "horizons": hs,
        "per_horizon": {str(h): per[h] for h in hs},
    }
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    EDGE_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("edge 模型訓練：各窗 %s",
                {h: (per[h].get("holdout_auc") if per[h].get("trained") else per[h].get("reason")) for h in hs})
    return meta


def score_candidates(candidates: list[dict[str, Any]]) -> dict[str, float]:
    """用主窗模型替當日候選打成功機率。candidates=[{symbol, stock_entry, side}]。無模型回空 dict。"""
    path = _model_path(_primary_h())
    if not settings.enable_edge_model or not path.exists() or not candidates:
        return {}
    # 紀律：主窗模型在保留樣本上沒展現 edge（AUC <= 門檻）就不重排，避免無效模型誤導選股。
    pm = ((_load_json(EDGE_META_PATH) or {}).get("per_horizon") or {}).get(str(_primary_h())) or {}
    auc = pm.get("holdout_auc")
    if auc is None or auc < EDGE_MIN_AUC:
        logger.info("edge 主窗 AUC=%s 未達 %.2f，本次不以模型重排候選", auc, EDGE_MIN_AUC)
        return {}
    try:
        import joblib  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        bundle = joblib.load(path)
        model, cols = bundle["model"], bundle["columns"]
        rows = [featurize(c.get("stock_entry", {}), c.get("side", "watchlist")) for c in candidates]
        X = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols).astype(float)
        probs = model.predict_proba(X)[:, 1]
        return {c["symbol"]: round(float(p), 3) for c, p in zip(candidates, probs)}
    except Exception as exc:  # noqa: BLE001 — 打分失敗不可阻斷晨報
        logger.warning("edge 打分失敗: %s", exc)
        return {}


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:  # noqa: BLE001
        return None
