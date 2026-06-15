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
from datetime import datetime
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

# 文字校準的最低樣本門檻（低於此不注入，避免雜訊誤導模型）
MIN_TEXT_SAMPLES = 12
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

def _training_samples() -> tuple[list[dict[str, Any]], list[int], list[str]]:
    """從所有 scorecard 組訓練集：X=featurize 向量、y=主窗方向是否正確、date 供時間序切分。"""
    h = _primary_h()
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
    """訓練本地 edge 模型（HistGradientBoosting）。樣本不足/單一類別→跳過。回 meta。"""
    if not settings.enable_edge_model:
        return {"trained": False, "reason": "disabled"}
    try:
        import joblib  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: PLC0415
        from sklearn.inspection import permutation_importance  # noqa: PLC0415
        from sklearn.metrics import accuracy_score, roc_auc_score  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — 套件未裝（如尚未 rebuild 容器）→ 靜默跳過
        logger.warning("edge 模型套件未就緒，跳過訓練: %s", exc)
        return {"trained": False, "reason": f"import failed: {exc}"}

    X, y, dates = _training_samples()
    n = len(y)
    if n < settings.edge_model_min_samples:
        return {"trained": False, "reason": f"樣本不足 {n}/{settings.edge_model_min_samples}",
                "n_samples": n}
    if len(set(y)) < 2:
        return {"trained": False, "reason": "標籤僅單一類別", "n_samples": n}

    # 時間序排序，前 80% 訓練、後 20% 驗證（walk-forward，避免用未來資料評估）
    order = sorted(range(n), key=lambda i: dates[i])
    Xs = pd.DataFrame([X[i] for i in order], columns=FEATURE_COLUMNS).astype(float)
    ys = np.array([y[i] for i in order])
    split = max(1, int(n * 0.8))
    X_tr, X_te, y_tr, y_te = Xs.iloc[:split], Xs.iloc[split:], ys[:split], ys[split:]

    model = HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.05, max_depth=4, l2_regularization=1.0,
        random_state=42,
    )
    model.fit(X_tr, y_tr)

    holdout_acc = holdout_auc = None
    top_features: list[str] = []
    if len(y_te) >= 5 and len(set(y_te)) >= 1:
        pred = model.predict(X_te)
        holdout_acc = round(float(accuracy_score(y_te, pred)), 3)
        if len(set(y_te)) == 2:
            holdout_auc = round(float(roc_auc_score(y_te, model.predict_proba(X_te)[:, 1])), 3)
            try:
                imp = permutation_importance(model, X_te, y_te, n_repeats=5, random_state=42)
                ranked = sorted(zip(FEATURE_COLUMNS, imp.importances_mean), key=lambda t: t[1], reverse=True)
                top_features = [c for c, v in ranked if v > 0][:5] or [ranked[0][0]]
            except Exception:  # noqa: BLE001
                top_features = []

    # 用全部資料重新擬合（上線模型），存檔
    model.fit(Xs, ys)
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "columns": FEATURE_COLUMNS}, EDGE_MODEL_PATH)
    meta = {
        "trained": True,
        "trained_at": datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds"),
        "n_samples": n,
        "primary_horizon": _primary_h(),
        "holdout_accuracy": holdout_acc,
        "holdout_auc": holdout_auc,
        "top_features": top_features,
        "positive_rate": round(float(sum(y) / n), 3),
    }
    EDGE_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("edge 模型訓練完成：n=%d acc=%s auc=%s top=%s", n, holdout_acc, holdout_auc, top_features)
    return meta


def score_candidates(candidates: list[dict[str, Any]]) -> dict[str, float]:
    """替當日候選打成功機率。candidates=[{symbol, stock_entry, side}]。無模型回空 dict。

    回 {symbol: P(這個方向看法在主窗內成立)}，供 morning_brief 重排候選。
    """
    if not settings.enable_edge_model or not EDGE_MODEL_PATH.exists() or not candidates:
        return {}
    try:
        import joblib  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        bundle = joblib.load(EDGE_MODEL_PATH)
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
