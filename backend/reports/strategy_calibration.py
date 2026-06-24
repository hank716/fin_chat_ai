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
from reports.backtest import FEATURE_COLUMNS, META_FEATURE_COLUMNS, featurize

logger = logging.getLogger("ai-market-backend.strategy_calibration")

STRATEGY_DIR = Path(settings.local_storage_path) / "strategy"
CALIBRATION_PATH = STRATEGY_DIR / "calibration.json"
EDGE_MODEL_PATH = STRATEGY_DIR / "edge_model.pkl"
EDGE_META_PATH = STRATEGY_DIR / "edge_model_meta.json"
RISK_META_PATH = STRATEGY_DIR / "risk_model_meta.json"
RANK_META_PATH = STRATEGY_DIR / "rank_model_meta.json"
META_META_PATH = STRATEGY_DIR / "meta_model_meta.json"   # meta-labeling[5]：「該不該下手」模型
EVAL_PATH = STRATEGY_DIR / "evaluation.json"
# Qlib 離線 image（階段 3[8]）產出：方向分數 + meta（per-horizon rank_ic/risk_auc）。
# serving 端只讀這兩個 JSON、永不 import qlib——pyqlib 重且 Python 版本挑剔，隔離在獨立 image。
QLIB_META_PATH = STRATEGY_DIR / "qlib_meta.json"
QLIB_SCORES_DIR = STRATEGY_DIR / "qlib_scores"

# 成效量測：足以下定論所需的最少樣本與交易日數（跨多種行情才不被單一 regime 帶偏）
EVAL_MIN_DATES = 10

# 文字校準的最低樣本門檻（低於此不注入，避免雜訊誤導模型）
MIN_TEXT_SAMPLES = 12
# edge 模型只有在保留樣本上展現「確實有在做事」的預測力（AUC ≥ 此值）才拿來重排候選，否則不動。
# 設 0.55＝排除噪音帶：方向預測在 5–20 日液態股本就接近效率牆（OOS ~0.51–0.53 全是噪音，
# 例 h20 0.533 雖統計上略高於 0.5 但無選股價值），要 ≥0.55 才當「明顯 edge」啟用（對齊 _eval_verdict 判讀）。
EDGE_MIN_AUC = 0.55
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
    risk = _load_json(RISK_META_PATH) or {}
    risk_per = risk.get("per_horizon") or {}
    risk_aucs = {h: (risk_per.get(str(h)) or {}).get("holdout_auc") for h in hs}
    rank = _load_json(RANK_META_PATH) or {}
    rank_per = rank.get("per_horizon") or {}
    rank_ics = {h: (rank_per.get(str(h)) or {}).get("rank_ic") for h in hs}
    verdict = _eval_verdict(sufficient, len(dates), span, wl, cau, edge)
    if any(v is not None for v in risk_aucs.values()):
        verdict += ("回撤風險模型 OOS AUC " + "、".join(
            f"{h}日 {risk_aucs[h]}" for h in hs if risk_aucs[h] is not None)
            + "（>0.5 才有預測力；用於避雷側排序與偏多高風險標記）。")
    if any(v is not None for v in rank_ics.values()):
        verdict += ("報酬 rank 模型 OOS rank-IC " + "、".join(
            f"{h}日 {rank_ics[h]}" for h in hs if rank_ics[h] is not None)
            + f"（≥{settings.rank_ic_gate} 才重排偏多；液態股方向多半不過＝符合效率牆）。")
    qmeta = _load_json(QLIB_META_PATH) or {}
    qper = qmeta.get("per_horizon") or {}
    qics = {h: (qper.get(str(h)) or qper.get(h) or {}).get("rank_ic") for h in hs}
    if any(v is not None for v in qics.values()):
        verdict += ("Qlib Alpha158 OOS rank-IC " + "、".join(
            f"{h}日 {qics[h]}" for h in hs if qics[h] is not None)
            + f"（離線 image 算、≥{settings.rank_ic_gate} 才採用；測業界因子庫能否破方向牆）。")
    meta_m = _load_json(META_META_PATH) or {}
    meta_per = meta_m.get("per_horizon") or {}
    meta_aucs = {h: (meta_per.get(str(h)) or {}).get("holdout_auc") for h in hs}
    if any(v is not None for v in meta_aucs.values()):
        segs = []
        for h in hs:
            pr = (meta_per.get(str(h)) or {})
            if pr.get("holdout_auc") is not None:
                pk, br = pr.get("precision_at_k"), pr.get("base_rate")
                segs.append(f"{h}日 AUC {pr['holdout_auc']}"
                            + (f"／命中前10%={pk}(基準{br})" if pk is not None else ""))
        verdict += ("meta 下手模型 OOS " + "、".join(segs)
                    + f"（≥{settings.meta_model_min_auc} 才用於 sizing/過濾；拚『該不該下手』非方向）。")
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
        "risk_model": {"per_horizon_auc": risk_aucs, "trained": risk.get("trained")},
        "rank_model": {"per_horizon_rank_ic": rank_ics, "trained": rank.get("trained")},
        "verdict": verdict,
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
    """每個時間窗各自一顆方向模型（edge_model_5.pkl / edge_model_20.pkl）。"""
    return STRATEGY_DIR / f"edge_model_{h}.pkl"


def _risk_model_path(h: int) -> Path:
    """每個時間窗各自一顆回撤風險模型（risk_model_5.pkl / risk_model_20.pkl）。"""
    return STRATEGY_DIR / f"risk_model_{h}.pkl"


def _rank_model_path(h: int) -> Path:
    """每個時間窗各自一顆報酬 rank 模型（rank_model_5.pkl / rank_model_20.pkl）。"""
    return STRATEGY_DIR / f"rank_model_{h}.pkl"


def _meta_model_path(h: int) -> Path:
    """每個時間窗各自一顆 meta-labeling 模型（meta_model_5.pkl / meta_model_20.pkl）。"""
    return STRATEGY_DIR / f"meta_model_{h}.pkl"


def _live_samples(h: int) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    """從 scorecard 組某窗的『線上真選股』樣本：X=featurize、y=**超額方向**、date 供時間切分。

    標籤與歷史回放集一致＝相對大盤的超額方向對不對（偏多正類＝贏大盤 vs_index_pct>0、偏空正類
    ＝輸大盤 vs_index_pct<0），剝除大盤 beta。無 vs_index_pct 的列略過。
    """
    X: list[dict[str, Any]] = []
    y: list[int] = []
    dates: list[str] = []
    for sc in backtest.load_scorecards():
        d = sc.get("as_of") or sc.get("report_date") or ""
        for it in sc.get("items", []):
            r = it.get("horizons", {}).get(str(h))
            if not _VALID(r) or r.get("vs_index_pct") is None:
                continue
            side = it.get("side", "watchlist")
            excess = r["vs_index_pct"]
            feat = it.get("features") or featurize({}, side)
            X.append({c: feat.get(c) for c in FEATURE_COLUMNS})
            y.append(1 if (excess > 0 if side == "watchlist" else excess < 0) else 0)
            dates.append(d)
    return X, y, dates


def _live_meta_samples(h: int) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    """從 scorecard 組 meta-labeling 線上樣本：X=featurize+conviction、y=**交易是否成功**(outcome=target_hit)。

    meta 標籤＝這筆訊號實際有沒有先觸目標(路徑感知，與歷史 fwd_tb 同精神)；conviction＝線上選股當下
    幾個訊號桶共振(scorecard 存的 signal_tags 數)。outcome 仍 pending/open/no_data 的列略過。
    """
    X: list[dict[str, Any]] = []
    y: list[int] = []
    dates: list[str] = []
    for sc in backtest.load_scorecards():
        d = sc.get("as_of") or sc.get("report_date") or ""
        for it in sc.get("items", []):
            r = it.get("horizons", {}).get(str(h))
            outcome = (r or {}).get("outcome")
            if outcome not in ("target_hit", "stop_hit", "open"):
                continue                           # 只取已有路徑結論的（pending/no_data 略過）
            side = it.get("side", "watchlist")
            feat = dict(it.get("features") or featurize({}, side))
            feat["conviction"] = len(it.get("signal_tags") or [])
            X.append({c: feat.get(c) for c in backtest.META_FEATURE_COLUMNS})
            y.append(1 if outcome == "target_hit" else 0)
            dates.append(d)
    return X, y, dates


# ── label-agnostic 共用核心：方向 edge / 回撤風險 / 後續(波動、殘差方向、meta) 都走它 ──

def _new_model():
    from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: PLC0415
    return HistGradientBoostingClassifier(
        max_iter=200, learning_rate=0.05, max_depth=4, l2_regularization=1.0, random_state=42,
    )


def _fit_predict(Xtr, ytr, Xte, wtr=None, feature_cols: list[str] = FEATURE_COLUMNS):
    """擬合一個 fold 並回 (model, used_cols, test 機率)。

    丟掉 train 子集裡少於 2 個相異非缺值的退化欄位（小 fold 易出現某特徵全 NaN／常數，
    HistGBT 分箱會炸 ValueError），並對 test 用同一組欄位預測。無可用欄位回 None。
    """
    cols = [c for c in feature_cols if Xtr[c].nunique(dropna=True) >= 2]
    if not cols:
        return None
    m = _new_model()
    m.fit(Xtr[cols], ytr, sample_weight=wtr)
    return m, cols, m.predict_proba(Xte[cols])[:, 1]


def _evaluate_oos(ds, h: int, *, with_importance: bool = True,
                  feature_cols: list[str] = FEATURE_COLUMNS) -> dict[str, Any]:
    """時間序 purged walk-forward + embargo 的 pooled OOS 評估（label-agnostic，支援樣本權重）。

    把唯一日期切成 K 段，逐段當 test、其前所有樣本當 train，並丟掉 train 中落在 test 起始日前
    h 個交易日內的列（前瞻窗會與 test 重疊→洩漏）。各 fold 的 test 機率 pool 起來算一個 AUC，
    比單次 80/20 切分穩定、且不被最後單一 regime 帶偏。資料太少則回退單次切分。
    另附 oos_y/oos_p 供 isotonic 機率校準[14]。
    """
    from sklearn.inspection import permutation_importance  # noqa: PLC0415
    from sklearn.metrics import accuracy_score, roc_auc_score  # noqa: PLC0415

    X = ds[feature_cols]
    yv = ds["__y"].to_numpy()
    wv = ds["__w"].to_numpy() if "__w" in ds.columns else None
    dates = sorted(ds["__d"].unique().tolist())
    nd = len(dates)
    pos = {d: i for i, d in enumerate(dates)}
    dpos = ds["__d"].map(pos).to_numpy()

    K = 5
    oos_y: list[int] = []
    oos_p: list[float] = []
    folds = 0
    last_fold: tuple = ()
    if nd >= 2 * K:
        edges = [round(nd * k / K) for k in range(K + 1)]
        for k in range(1, K):
            ts, te = edges[k], edges[k + 1]              # test 日期位置 [ts, te)
            if te <= ts:
                continue
            tr_mask = dpos < (ts - h)                     # embargo：丟掉 test 前 h 個交易日的 train
            te_mask = (dpos >= ts) & (dpos < te)
            ytr, yte = yv[tr_mask], yv[te_mask]
            if tr_mask.sum() < 50 or te_mask.sum() < 5 or len(set(ytr)) < 2 or len(set(yte)) < 2:
                continue
            wtr = wv[tr_mask] if wv is not None else None
            try:
                res = _fit_predict(X[tr_mask], ytr, X[te_mask], wtr, feature_cols)
            except Exception as exc:  # noqa: BLE001 — 單一 fold 退化不阻斷整體評估
                logger.debug("fold 跳過(h=%s,k=%s): %s", h, k, exc)
                continue
            if res is None:
                continue
            m, cols, p = res
            oos_p.extend(p.tolist())
            oos_y.extend(yte.tolist())
            last_fold = (m, X[te_mask][cols], yte)
            folds += 1

    if folds == 0 or len(set(oos_y)) < 2:                # 回退：單次 80/20 時間切分
        split = max(1, int(len(yv) * 0.8))
        yte = yv[split:]
        res = None
        if len(yte) >= 5 and len(set(yte)) == 2:
            wtr = wv[:split] if wv is not None else None
            try:
                res = _fit_predict(X.iloc[:split], yv[:split], X.iloc[split:], wtr, feature_cols)
            except Exception as exc:  # noqa: BLE001
                logger.debug("單次切分失敗(h=%s): %s", h, exc)
        if res is None:
            return {"auc": None, "acc": None, "top": [], "eval_method": "insufficient",
                    "cv_folds": 0, "oos_y": [], "oos_p": []}
        m, cols, p = res
        oos_p, oos_y = p.tolist(), yte.tolist()
        last_fold = (m, X.iloc[split:][cols], yte)
        method = "single_split"
    else:
        method = "purged_walk_forward"

    auc = round(float(roc_auc_score(oos_y, oos_p)), 3)
    preds = [1 if p >= 0.5 else 0 for p in oos_p]
    acc = round(float(accuracy_score(oos_y, preds)), 3)
    prec_k, base = _precision_at_k(oos_y, oos_p)
    top: list[str] = []
    if last_fold and with_importance:
        try:
            m, Xte, yte = last_fold
            imp = permutation_importance(m, Xte, yte, n_repeats=5, random_state=42)
            ranked = sorted(zip(list(Xte.columns), imp.importances_mean), key=lambda t: t[1], reverse=True)
            top = [c for c, v in ranked if v > 0][:5] or [ranked[0][0]]
        except Exception:  # noqa: BLE001
            top = []
    return {"auc": auc, "acc": acc, "top": top, "eval_method": method, "cv_folds": folds,
            "precision_at_k": prec_k, "base_rate": base, "oos_y": oos_y, "oos_p": oos_p}


def _precision_at_k(oos_y: list[int], oos_p: list[float], frac: float = 0.1) -> tuple[float | None, float | None]:
    """meta-labeling[5] 過濾才看的指標：取機率最高的前 frac 樣本，回 (其正類比率, 全體基準率)。

    precision@K 明顯 > base_rate ＝『只下手高把握的』確實提升命中＝sizing/過濾有用（即使 AUC 平庸）。
    """
    n = len(oos_y)
    if n < 20:
        return None, None
    k = max(1, int(n * frac))
    order = sorted(range(n), key=lambda i: oos_p[i], reverse=True)[:k]
    prec = sum(oos_y[i] for i in order) / k
    base = sum(oos_y) / n
    return round(float(prec), 3), round(float(base), 3)


def _fit_calibrator(oos_y: list[int], oos_p: list[float]):
    """用 pooled OOS 預測擬 isotonic 機率校準器[14]（提升機率可信度/準確度，不改 AUC）。資料太少回 None。"""
    if len(oos_y) < 50 or len(set(oos_y)) < 2:
        return None
    try:
        import numpy as np  # noqa: PLC0415
        from sklearn.isotonic import IsotonicRegression  # noqa: PLC0415
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(np.asarray(oos_p, dtype=float), np.asarray(oos_y, dtype=float))
        return iso
    except Exception as exc:  # noqa: BLE001
        logger.debug("isotonic 校準失敗: %s", exc)
        return None


def _build_dataset(h: int, label_col: str, *, use_live: bool,
                   feature_cols: list[str] = FEATURE_COLUMNS, live_fn=None):
    """某窗的訓練集：回 ds(feature_cols + __y + __d + __w)，按時間排序；無資料回 None。

    歷史回放集帶 `weight`（重疊去重[7]）；線上樣本權重 1.0。缺標籤的列略過。
    live_fn＝取線上樣本的函式（預設 _live_samples 走超額方向；meta 走 _live_meta_samples）。
    """
    import pandas as pd  # noqa: PLC0415

    from reports import training_set  # noqa: PLC0415

    frames = []
    hist = training_set.load_training_set(h)
    if not hist.empty and label_col in hist.columns and all(c in hist.columns for c in feature_cols):
        hd = hist[feature_cols].astype(float).copy()
        hd["__y"] = pd.to_numeric(hist[label_col], errors="coerce").to_numpy()
        hd["__d"] = hist["as_of"].astype(str).to_numpy()
        hd["__w"] = (hist["weight"].astype(float).to_numpy() if "weight" in hist.columns else 1.0)
        hd = hd[hd["__y"].notna()].copy()
        hd["__y"] = hd["__y"].astype(int)
        frames.append(hd)
    if use_live:
        Xl, yl, dl = (live_fn or _live_samples)(h)
        if Xl:
            ld = pd.DataFrame(Xl, columns=feature_cols).astype(float)
            ld["__y"] = yl
            ld["__d"] = dl
            ld["__w"] = 1.0
            frames.append(ld)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True).sort_values("__d").reset_index(drop=True)


def _train_target(label_col: str, model_path_fn, meta_path: Path, *, use_live: bool,
                  enabled: bool, name: str, feature_cols: list[str] = FEATURE_COLUMNS,
                  live_fn=None) -> dict[str, Any]:
    """通用 per-horizon 訓練：吃指定標籤欄，各窗訓練 HistGBT + isotonic 校準，落地模型與 meta。"""
    if not enabled:
        return {"trained": False, "reason": "disabled"}
    try:
        import joblib  # noqa: PLC0415
        import sklearn  # noqa: F401,PLC0415 — 確認套件就緒（尚未 rebuild 容器則靜默跳過）
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s 模型套件未就緒，跳過訓練: %s", name, exc)
        return {"trained": False, "reason": f"import failed: {exc}"}

    minimum = settings.edge_model_min_samples

    def _train_one(h: int) -> dict[str, Any]:
        ds = _build_dataset(h, label_col, use_live=use_live, feature_cols=feature_cols, live_fn=live_fn)
        n = 0 if ds is None else len(ds)
        if ds is None or n < minimum or ds["__y"].nunique() < 2:
            reason = (f"樣本不足 {n}/{minimum}" if (ds is None or n < minimum)
                      else f"標籤僅單一類別（n={n}）")
            return {"trained": False, "reason": reason, "n_samples": n,
                    "min_samples": minimum, "horizon": h}
        ev = _evaluate_oos(ds, h, feature_cols=feature_cols)
        yv = ds["__y"].to_numpy()
        wv = ds["__w"].to_numpy() if "__w" in ds.columns else None
        X = ds[feature_cols]
        # 丟掉全 NaN／常數欄（如未爬到的基本面、全市場無值的 sector_rs）——HistGBT 分箱會在
        # 零非缺值欄炸 ValueError；存「實際用到的欄」供打分時對齊。
        cols = [c for c in feature_cols if X[c].nunique(dropna=True) >= 2]
        model = _new_model()
        model.fit(X[cols], yv, sample_weight=wv)   # 全資料重擬合上線
        calibrator = _fit_calibrator(ev["oos_y"], ev["oos_p"])
        STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": model, "columns": cols, "calibrator": calibrator},
                    model_path_fn(h))
        return {"trained": True, "n_samples": n, "min_samples": minimum, "horizon": h,
                "holdout_accuracy": ev["acc"], "holdout_auc": ev["auc"], "top_features": ev["top"],
                "eval_method": ev["eval_method"], "cv_folds": ev["cv_folds"],
                "precision_at_k": ev.get("precision_at_k"), "base_rate": ev.get("base_rate"),
                "calibrated": calibrator is not None,
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
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("%s 模型訓練：各窗 %s", name,
                {h: (per[h].get("holdout_auc") if per[h].get("trained") else per[h].get("reason")) for h in hs})
    return meta


def train_edge_model() -> dict[str, Any]:
    """訓練本地方向 edge 模型——每窗各一顆，吃『歷史回放選股集 + 線上真選股』，標籤＝超額方向。

    歷史集（reports.training_set）讓樣本一次到位、不必等數週；兩者合併後做 purged walk-forward。
    """
    return _train_target("label", _model_path, EDGE_META_PATH,
                         use_live=True, enabled=settings.enable_edge_model, name="edge")


def train_risk_model() -> dict[str, Any]:
    """訓練本地回撤風險模型——每窗各一顆，標籤＝未來 h 日最大回撤(MAE)橫斷面中位數切分。

    與方向 edge 並存、不取代；只吃歷史回放集（與離線實驗 apples-to-apples）。OOS AUC 通常遠高於
    方向模型（波動度持續性），用於避雷側排序與偏多高風險標記，見 score_risk。
    """
    return _train_target("risk_label", _risk_model_path, RISK_META_PATH,
                         use_live=False, enabled=settings.enable_risk_model, name="risk")


def train_meta_model() -> dict[str, Any]:
    """訓練 meta-labeling 模型——標籤＝這筆訊號『該不該下手』(triple-barrier 是否先觸目標)，
    特徵＝基準特徵 + conviction(訊號共振)。歷史回放 bootstrap + 線上 scorecard 增量。

    不拚方向（那是 edge 模型的事、已撞牆）；拚『下手成功率』供 sizing/過濾。gate=meta_model_min_auc，
    另回 precision@K（過濾才看的指標）。見 score_meta。
    """
    return _train_target("meta_label", _meta_model_path, META_META_PATH,
                         use_live=True, enabled=settings.enable_meta_model, name="meta",
                         feature_cols=META_FEATURE_COLUMNS, live_fn=_live_meta_samples)


# ───────────────── 報酬 rank 模型（Phase 2[12]：回歸殘差報酬，rank-IC 評估）─────────────────

def _new_regressor():
    from sklearn.ensemble import HistGradientBoostingRegressor  # noqa: PLC0415
    return HistGradientBoostingRegressor(
        max_iter=200, learning_rate=0.05, max_depth=4, l2_regularization=1.0, random_state=42,
    )


def _build_rank_dataset(h: int):
    """rank 模型訓練集：X=FEATURE_COLUMNS、__t=因子中性化殘差報酬(連續)、__d 日期、__w 權重。"""
    import pandas as pd  # noqa: PLC0415

    from reports import training_set  # noqa: PLC0415

    hist = training_set.load_training_set(h)
    if hist.empty or "resid_return_pct" not in hist.columns:
        return None
    ds = hist[FEATURE_COLUMNS].astype(float).copy()
    ds["__t"] = pd.to_numeric(hist["resid_return_pct"], errors="coerce").to_numpy()
    ds["__d"] = hist["as_of"].astype(str).to_numpy()
    ds["__w"] = (hist["weight"].astype(float).to_numpy() if "weight" in hist.columns else 1.0)
    ds = ds[ds["__t"].notna()].reset_index(drop=True)
    return ds.sort_values("__d").reset_index(drop=True) if len(ds) else None


def _evaluate_rank_oos(ds, h: int) -> dict[str, Any]:
    """purged walk-forward + embargo 的 rank-IC 評估：逐 fold 回歸殘差報酬，預測 pool 後算 IC。

    rank_ic＝**逐日 Spearman(預測, 實際殘差) 的平均**（液態股池每日 N 小，逐日才是真 IC）；
    icir＝逐日 IC 的 mean/std。另回 pooled Spearman 當輔助。資料太少回 rank_ic=None。
    """
    import numpy as np  # noqa: PLC0415
    from scipy.stats import spearmanr  # noqa: PLC0415

    cols_all = ds[FEATURE_COLUMNS]
    tv = ds["__t"].to_numpy()
    wv = ds["__w"].to_numpy()
    sv = ds["side_bull"].to_numpy() if "side_bull" in ds.columns else np.zeros(len(ds))
    dates = sorted(ds["__d"].unique().tolist())
    nd = len(dates)
    pos = {d: i for i, d in enumerate(dates)}
    dpos = ds["__d"].map(pos).to_numpy()
    dser = ds["__d"].to_numpy()

    K = 5
    oos_t: list[float] = []
    oos_p: list[float] = []
    oos_key: list[tuple] = []          # (date, side)：IC 在「同日同側」內算才是 watchlist 重排該看的鑑別力
    folds = 0
    if nd >= 2 * K:
        edges = [round(nd * k / K) for k in range(K + 1)]
        for k in range(1, K):
            ts, te = edges[k], edges[k + 1]
            if te <= ts:
                continue
            tr_mask = dpos < (ts - h)                 # embargo
            te_mask = (dpos >= ts) & (dpos < te)
            if tr_mask.sum() < 50 or te_mask.sum() < 5:
                continue
            cols = [c for c in FEATURE_COLUMNS if cols_all[c][tr_mask].nunique(dropna=True) >= 2]
            if not cols:
                continue
            try:
                m = _new_regressor()
                m.fit(cols_all[cols][tr_mask], tv[tr_mask], sample_weight=wv[tr_mask])
                p = m.predict(cols_all[cols][te_mask])
            except Exception as exc:  # noqa: BLE001 — 單 fold 退化不阻斷
                logger.debug("rank fold 跳過(h=%s,k=%s): %s", h, k, exc)
                continue
            oos_p.extend(p.tolist())
            oos_t.extend(tv[te_mask].tolist())
            oos_key.extend(zip(dser[te_mask].tolist(), sv[te_mask].tolist()))
            folds += 1

    if folds == 0 or len(oos_p) < 20:
        return {"rank_ic": None, "icir": None, "pooled_ic": None,
                "eval_method": "insufficient", "cv_folds": 0}

    # 逐「日×側」算 Spearman：重排 watchlist 看的是同側內的排序鑑別力，混 bull/bear 會被
    # 漲家 vs 跌家的前瞻價差(side_bull 特徵)灌水、高估對單側重排的用處。
    grp: dict[tuple, tuple[list, list]] = {}
    for key, t, p in zip(oos_key, oos_t, oos_p):
        grp.setdefault(key, ([], []))
        grp[key][0].append(t)
        grp[key][1].append(p)
    daily: list[float] = []
    for (_key), (ts_, ps_) in grp.items():
        if len(ts_) >= 3 and len(set(ps_)) > 1 and len(set(ts_)) > 1:
            ic, _ = spearmanr(ps_, ts_)
            if ic == ic:  # not NaN
                daily.append(float(ic))
    rank_ic = round(float(np.mean(daily)), 4) if daily else None
    icir = round(float(np.mean(daily) / np.std(daily)), 3) if len(daily) > 1 and np.std(daily) > 0 else None
    pooled, _ = spearmanr(oos_p, oos_t)
    return {"rank_ic": rank_ic, "icir": icir,
            "pooled_ic": round(float(pooled), 4) if pooled == pooled else None,
            "eval_method": "purged_walk_forward", "cv_folds": folds, "n_days_ic": len(daily)}


def train_rank_model() -> dict[str, Any]:
    """訓練報酬 rank 模型——每窗各一顆，回歸因子中性化殘差報酬，以 rank-IC 評估。

    learning-to-rank 取向（比二元分類更貼合「排序多頭」）。液態股 5–20 日方向多半 rank-IC<門檻、
    不會啟用重排（與效率牆一致＝預期且正確）。
    """
    if not settings.enable_rank_model:
        return {"trained": False, "reason": "disabled"}
    try:
        import joblib  # noqa: PLC0415
        import sklearn  # noqa: F401,PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("rank 模型套件未就緒，跳過訓練: %s", exc)
        return {"trained": False, "reason": f"import failed: {exc}"}

    minimum = settings.edge_model_min_samples

    def _one(h: int) -> dict[str, Any]:
        ds = _build_rank_dataset(h)
        n = 0 if ds is None else len(ds)
        if ds is None or n < minimum:
            return {"trained": False, "reason": f"樣本不足 {n}/{minimum}", "n_samples": n, "horizon": h}
        ev = _evaluate_rank_oos(ds, h)
        cols = [c for c in FEATURE_COLUMNS if ds[c].nunique(dropna=True) >= 2]
        model = _new_regressor()
        model.fit(ds[cols], ds["__t"].to_numpy(), sample_weight=ds["__w"].to_numpy())
        STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": model, "columns": cols}, _rank_model_path(h))
        return {"trained": True, "n_samples": n, "horizon": h,
                "rank_ic": ev["rank_ic"], "icir": ev["icir"], "pooled_ic": ev.get("pooled_ic"),
                "eval_method": ev["eval_method"], "cv_folds": ev["cv_folds"]}

    hs = backtest.horizons()
    per = {h: _one(h) for h in hs}
    ph = _primary_h()
    meta = {
        **per.get(ph, {"trained": False, "n_samples": 0}),
        "trained_at": datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds"),
        "primary_horizon": ph, "horizons": hs,
        "per_horizon": {str(h): per[h] for h in hs},
    }
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    RANK_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("rank 模型訓練：各窗 rank-IC %s",
                {h: (per[h].get("rank_ic") if per[h].get("trained") else per[h].get("reason")) for h in hs})
    return meta


def score_rank(candidates: list[dict[str, Any]]) -> dict[str, float]:
    """報酬 rank 打分：回 symbol→預測殘差報酬分數（過 rank_ic_gate 的窗才納入，否則空 dict）。

    多窗以 rank-IC 為權重加權平均；用於重排偏多 watchlist（高分在前）。液態股多半不過 gate＝不重排。
    """
    if not settings.enable_rank_model or not candidates:
        return {}
    per = (_load_json(RANK_META_PATH) or {}).get("per_horizon") or {}
    usable = []
    for h in backtest.horizons():
        ric = (per.get(str(h)) or {}).get("rank_ic")
        if ric is not None and ric >= settings.rank_ic_gate and _rank_model_path(h).exists():
            usable.append((h, ric))
    if not usable:
        rics = {h: (per.get(str(h)) or {}).get("rank_ic") for h in backtest.horizons()}
        logger.info("rank 各窗 rank-IC=%s 皆未達 %.3f，本次不以 rank 重排", rics, settings.rank_ic_gate)
        return {}
    try:
        import joblib  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
        rows = [featurize(c.get("stock_entry", {}), c.get("side", "watchlist")) for c in candidates]
        agg: dict[str, float] = {c["symbol"]: 0.0 for c in candidates}
        wsum = 0.0
        for h, ric in usable:
            bundle = joblib.load(_rank_model_path(h))
            model, cols = bundle["model"], bundle["columns"]
            X = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols).astype(float)
            preds = model.predict(X)
            wsum += ric
            for c, p in zip(candidates, preds):
                agg[c["symbol"]] += ric * float(p)
        return {sym: round(v / wsum, 4) for sym, v in agg.items()} if wsum > 0 else {}
    except Exception as exc:  # noqa: BLE001 — 打分失敗不可阻斷晨報
        logger.warning("rank 打分失敗: %s", exc)
        return {}


def _latest_qlib_scores() -> dict[str, float]:
    """讀最近一份 qlib_scores/{date}.json → {symbol: 分數}；無檔/壞檔回空 dict。"""
    if not QLIB_SCORES_DIR.exists():
        return {}
    files = sorted(QLIB_SCORES_DIR.glob("*.json"))
    if not files:
        return {}
    data = _load_json(files[-1]) or {}
    return {str(k): float(v) for k, v in (data.get("scores") or {}).items()}


def score_qlib(candidates: list[dict[str, Any]]) -> dict[str, float]:
    """Qlib 方向打分：讀離線 image 產的 JSON，過 rank_ic_gate 的窗才回，否則空 dict。

    與 score_rank 同紀律（無 edge 不重排），差別是分數由獨立 qlib image 離線預算好寫檔，
    serving 端**只讀 JSON、永不 import qlib**（pyqlib 隔離在獨立容器）。缺檔/未過 gate 回空。
    """
    if not settings.enable_qlib or not candidates:
        return {}
    try:
        meta = _load_json(QLIB_META_PATH) or {}
        per = meta.get("per_horizon") or {}
        # 任一窗 rank-IC ≥ gate 才採用（沿用 rank 家族 gate）。
        passes = any((per.get(str(h)) or per.get(h) or {}).get("rank_ic") is not None
                     and (per.get(str(h)) or per.get(h) or {}).get("rank_ic") >= settings.rank_ic_gate
                     for h in backtest.horizons())
        if not passes:
            rics = {h: (per.get(str(h)) or per.get(h) or {}).get("rank_ic") for h in backtest.horizons()}
            logger.info("qlib 各窗 rank-IC=%s 皆未達 %.3f，本次不以 qlib 重排", rics, settings.rank_ic_gate)
            return {}
        scores = _latest_qlib_scores()
        if not scores:
            logger.info("qlib 過 gate 但無 scores 檔（離線 image 尚未產出），不重排")
            return {}
        wanted = {c["symbol"] for c in candidates}
        return {sym: round(v, 6) for sym, v in scores.items() if sym in wanted}
    except Exception as exc:  # noqa: BLE001 — 打分失敗不可阻斷晨報
        logger.warning("qlib 打分失敗: %s", exc)
        return {}


def _score_with(candidates: list[dict[str, Any]], meta_path: Path, model_path_fn,
                min_auc: float, enrich=None) -> dict[str, float]:
    """用**所有過 gate 的時間窗**模型替候選打機率（含 isotonic 校準），(auc−0.5) 加權平均成 symbol→prob。

    每窗各自一顆模型、各自 gate：只要某窗 OOS AUC ≥ 門檻就納入。全部都不過才回空 dict
    （維持「無預測力不啟用」的保護）。candidates=[{symbol, stock_entry, side}]。
    enrich(candidate, row)＝可選的特徵補充（如 meta 注入 conviction，featurize 不產的欄）。
    """
    per = (_load_json(meta_path) or {}).get("per_horizon") or {}
    usable = []
    for h in backtest.horizons():
        auc = (per.get(str(h)) or {}).get("holdout_auc")
        if auc is not None and auc >= min_auc and model_path_fn(h).exists():
            usable.append((h, auc))
    if not usable:
        aucs = {h: (per.get(str(h)) or {}).get("holdout_auc") for h in backtest.horizons()}
        logger.info("各窗 AUC=%s 皆未達 %.2f，本次不啟用該模型", aucs, min_auc)
        return {}
    import joblib  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415
    rows = [featurize(c.get("stock_entry", {}), c.get("side", "watchlist")) for c in candidates]
    if enrich is not None:
        for c, r in zip(candidates, rows):
            enrich(c, r)
    agg: dict[str, float] = {c["symbol"]: 0.0 for c in candidates}
    wsum = 0.0
    for h, auc in usable:
        bundle = joblib.load(model_path_fn(h))
        model, cols = bundle["model"], bundle["columns"]
        calibrator = bundle.get("calibrator")
        X = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows], columns=cols).astype(float)
        probs = model.predict_proba(X)[:, 1]
        if calibrator is not None:
            probs = np.clip(calibrator.transform(probs), 0.0, 1.0)
        w = auc - 0.5                              # AUC 越高權重越大
        wsum += w
        for c, p in zip(candidates, probs):
            agg[c["symbol"]] += w * float(p)
    return {sym: round(v / wsum, 3) for sym, v in agg.items()} if wsum > 0 else {}


def score_candidates(candidates: list[dict[str, Any]]) -> dict[str, float]:
    """方向 edge 打分：回 symbol→贏大盤的超額機率（過 EDGE_MIN_AUC gate 才回，否則空 dict）。"""
    if not settings.enable_edge_model or not candidates:
        return {}
    try:
        return _score_with(candidates, EDGE_META_PATH, _model_path, EDGE_MIN_AUC)
    except Exception as exc:  # noqa: BLE001 — 打分失敗不可阻斷晨報
        logger.warning("edge 打分失敗: %s", exc)
        return {}


def score_risk(candidates: list[dict[str, Any]]) -> dict[str, float]:
    """回撤風險打分：回 symbol→未來深跌機率（過 risk_model_min_auc gate 才回，否則空 dict）。

    用於避雷側(caution)排序與偏多清單高風險標記，與方向 edge 分離、不重排偏多選股。
    """
    if not settings.enable_risk_model or not candidates:
        return {}
    try:
        return _score_with(candidates, RISK_META_PATH, _risk_model_path, settings.risk_model_min_auc)
    except Exception as exc:  # noqa: BLE001 — 打分失敗不可阻斷晨報
        logger.warning("風險打分失敗: %s", exc)
        return {}


def score_meta(candidates: list[dict[str, Any]]) -> dict[str, float]:
    """meta-labeling 打分：回 symbol→P(這筆訊號會成功)（過 meta_model_min_auc gate 才回，否則空 dict）。

    用於 sizing/過濾（不重排方向）：高 P＝高把握可加重、低 P＝降權標「低把握」。candidate 的
    stock_entry["conviction"] 由 morning_brief 注入（命中幾個選股清單），featurize 不產故在此補。
    """
    if not settings.enable_meta_model or not candidates:
        return {}

    def _enrich(c: dict[str, Any], row: dict[str, Any]) -> None:
        row["conviction"] = (c.get("stock_entry", {}) or {}).get("conviction")

    try:
        return _score_with(candidates, META_META_PATH, _meta_model_path,
                           settings.meta_model_min_auc, enrich=_enrich)
    except Exception as exc:  # noqa: BLE001 — 打分失敗不可阻斷晨報
        logger.warning("meta 打分失敗: %s", exc)
        return {}


def run_battlefield_experiment(smallcap_min: float = 5_000_000) -> dict[str, Any]:
    """改戰場另池實驗[10]：比較主池 / 中小型股 / 事件窗(PEAD) 三個池子的 edge/risk/rank OOS 表現。

    純離線、不存模型、不動晨報——只回報「哪個戰場的方向訊號比較強」，供決定是否值得上線。
    重用同一套 purged walk-forward 核心（_evaluate_oos / _evaluate_rank_oos），apples-to-apples。
    """
    import pandas as pd  # noqa: PLC0415

    from reports import training_set as T  # noqa: PLC0415

    hs = backtest.horizons()
    big = T._build_big(hs)
    if big is None or big.empty:
        return {"ran": False, "reason": "no usable symbols"}

    main = T._emit_all(big, hs, min_amount=T.MIN_AMOUNT_TWD, max_amount=None)
    smallcap = T._emit_all(big, hs, min_amount=smallcap_min, max_amount=T.MIN_AMOUNT_TWD)
    event = main[main["days_since_earnings"].notna() & (main["days_since_earnings"] <= 20)] \
        if "days_since_earnings" in main.columns else main.iloc[0:0]

    def _assemble(df: pd.DataFrame, col: str, kind: str):
        sub = df[df[col].notna()]
        if sub.empty:
            return None
        ds = sub[FEATURE_COLUMNS].astype(float).copy()
        ds["__d"] = sub["as_of"].astype(str).to_numpy()
        ds["__w"] = sub["weight"].astype(float).to_numpy() if "weight" in sub.columns else 1.0
        if kind == "cls":
            ds["__y"] = sub[col].astype(int).to_numpy()
        else:
            ds["__t"] = sub[col].astype(float).to_numpy()
        return ds

    def _metrics(df_h: pd.DataFrame, h: int) -> dict[str, Any]:
        out: dict[str, Any] = {"n": int(len(df_h))}
        for tag, col in (("edge_auc", "label"), ("risk_auc", "risk_label")):
            ds = _assemble(df_h, col, "cls")
            out[tag] = _evaluate_oos(ds, h, with_importance=False)["auc"] \
                if ds is not None and ds["__y"].nunique() >= 2 else None
        ds = _assemble(df_h, "resid_return_pct", "reg")
        rk = _evaluate_rank_oos(ds, h) if ds is not None else {}
        out["rank_ic"] = rk.get("rank_ic")
        out["rank_icir"] = rk.get("icir")          # 一致性：逐日×側 IC 的 mean/std（>0.3 才算穩）
        out["rank_pooled_ic"] = rk.get("pooled_ic")
        out["rank_n_days"] = rk.get("n_days_ic")
        return out

    results: dict[str, Any] = {}
    for name, ds_all in (("main", main), ("smallcap", smallcap), ("event_pead", event)):
        for h in hs:
            df_h = ds_all[ds_all["horizon"] == h] if not ds_all.empty else ds_all
            results[f"{name}_{h}"] = _metrics(df_h, h) if len(df_h) else {"n": 0}
    out = {"ran": True, "smallcap_band": [smallcap_min, T.MIN_AMOUNT_TWD],
           "event_window_days": 20, "generated_at": datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds"),
           "results": results}
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    (STRATEGY_DIR / "battlefield_experiment.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")  # 持久化：curl timeout 也讀得到
    return out


def backtest_smallcap_sleeve(*, smallcap_min: float = 5_000_000, top_k: int = 3,
                             costs_pct: tuple[float, ...] = (0.4, 0.6, 0.9, 1.2),
                             horizon: int | None = None) -> dict[str, Any]:
    """小型股 5 日 rank sleeve 的「扣成本淨報酬」回測（純離線、不存模型、不動晨報）。

    OOS purged walk-forward：對小型股(5M–50M)偏多候選，逐折用過去資料訓練殘差 rank 回歸→預測未來，
    每日取預測 top-K（long-only sleeve 實務做法），算其**實際** 5 日報酬，對比「全池等權」基準與底部 K。
    再在多個來回成本水準下看淨報酬，回答「0.06 的 IC 扣成本後還剩不剩」。
    成本＝來回總成本%（手續費買賣＋賣方證交稅0.3%＋小型股滑價）；long-short 扣兩腿成本（且空小型股難，僅供參考）。
    """
    import numpy as np  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415

    from reports import training_set as T  # noqa: PLC0415

    h = horizon or backtest.horizons()[0]
    big = T._build_big([h])
    if big is None or big.empty:
        return {"ran": False, "reason": "no usable symbols"}
    sc = T._emit_all(big, [h], min_amount=smallcap_min, max_amount=T.MIN_AMOUNT_TWD)
    df = sc[(sc["horizon"] == h) & sc["resid_return_pct"].notna() & sc["fwd_return_pct"].notna()].copy()
    df = df.sort_values("as_of").reset_index(drop=True)
    if len(df) < 500:
        return {"ran": False, "reason": f"too few rows {len(df)}"}

    dates = sorted(df["as_of"].unique().tolist())
    nd = len(dates)
    pos = {d: i for i, d in enumerate(dates)}
    dpos = df["as_of"].map(pos).to_numpy()
    X = df[FEATURE_COLUMNS].astype(float)
    tgt = df["resid_return_pct"].astype(float).to_numpy()
    wv = df["weight"].astype(float).to_numpy() if "weight" in df.columns else None
    pred = np.full(len(df), np.nan)

    K = 5
    edges = [round(nd * k / K) for k in range(K + 1)]
    for k in range(1, K):
        ts, te = edges[k], edges[k + 1]
        if te <= ts:
            continue
        tr = dpos < (ts - h)                       # embargo h 天
        tem = (dpos >= ts) & (dpos < te)
        if tr.sum() < 50 or tem.sum() < 5:
            continue
        cols = [c for c in FEATURE_COLUMNS if X[c][tr].nunique(dropna=True) >= 2]
        if not cols:
            continue
        m = _new_regressor()
        m.fit(X[cols][tr], tgt[tr], sample_weight=(wv[tr] if wv is not None else None))
        pred[np.where(tem)[0]] = m.predict(X[cols][tem])

    df["pred"] = pred
    oos = df[df["pred"].notna() & (df["side"] == "watchlist")]   # long-only：只取偏多側
    rows = []
    for _d, g in oos.groupby("as_of"):
        if len(g) < 2:
            continue
        g = g.sort_values("pred", ascending=False)
        kk = min(top_k, len(g))
        rows.append((g.head(kk)["fwd_return_pct"].mean(), g.tail(kk)["fwd_return_pct"].mean(),
                     g["fwd_return_pct"].mean(), len(g)))
    if len(rows) < 20:
        return {"ran": False, "reason": f"too few oos days {len(rows)}"}
    bt = pd.DataFrame(rows, columns=["top", "bot", "pool", "n"])

    gross_top = float(bt["top"].mean())
    gross_pool = float(bt["pool"].mean())
    gross_ls = float((bt["top"] - bt["bot"]).mean())
    alpha = gross_top - gross_pool                  # 選股 alpha（top-K vs 全池，買賣成本相抵）
    out = {
        "ran": True, "horizon": h, "smallcap_band": [smallcap_min, T.MIN_AMOUNT_TWD], "top_k": top_k,
        "oos_days": len(bt), "avg_candidates_per_day": round(float(bt["n"].mean()), 1),
        "gross_top_5d_pct": round(gross_top, 3),
        "gross_pool_5d_pct": round(gross_pool, 3),
        "selection_alpha_5d_pct": round(alpha, 3),
        "longshort_gross_5d_pct": round(gross_ls, 3),
        "hit_rate_top_gt_pool": round(float((bt["top"] > bt["pool"]).mean()), 3),
        # 淨報酬：每筆 5 日持有，扣來回成本%。long-short 扣兩腿。
        "net_top_5d_pct": {f"cost{c}": round(gross_top - c, 3) for c in costs_pct},
        "net_longshort_5d_pct": {f"cost{c}": round(gross_ls - 2 * c, 3) for c in costs_pct},
        # 粗略年化（假設每 5 日換手≈50 期/年；重疊持有故僅供量級參考）
        "ann_net_top_pct_approx": {f"cost{c}": round((gross_top - c) * 50, 1) for c in costs_pct},
        "note": "long-only top-K 為實務可行；long-short 需借券放空小型股（成本高/難借），僅供參考。年化為粗估。",
        "generated_at": datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds"),
    }
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    (STRATEGY_DIR / "smallcap_sleeve_backtest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


SIZING_BACKTEST_PATH = STRATEGY_DIR / "sizing_backtest.json"
_SIZING_SCHEMES = ("equal", "meta", "inv_risk", "combo", "vol_target")


def _size_weights(items: list[dict[str, Any]], scheme: str, *, max_w: float = 0.30) -> list[float]:
    """把分數合成 long-only 正規化部位權重（和=1、單檔上限 max_w）。共用於回測與 serving。

    items=[{meta_p, risk_p, vol}]。方案：equal/meta(∝meta_p)/inv_risk(∝1−risk_p)/
    combo(∝meta_p·(1−risk_p))/vol_target(∝1/vol)。缺值以中性值代入；全 0 退回等權。
    """
    import numpy as np  # noqa: PLC0415

    n = len(items)
    if n == 0:
        return []

    def _meta(it):
        return max(float(it.get("meta_p") or 0.0), 0.0)

    def _invrisk(it):
        rp = it.get("risk_p")
        return max(1.0 - (float(rp) if rp is not None else 0.5), 0.0)

    def _vol(it):
        v = it.get("vol")
        return (1.0 / max(float(v), 1e-6)) if v not in (None, 0) and float(v) > 0 else np.nan

    if scheme == "meta":
        raw = np.array([_meta(it) for it in items])
    elif scheme == "inv_risk":
        raw = np.array([_invrisk(it) for it in items])
    elif scheme == "combo":
        raw = np.array([_meta(it) * _invrisk(it) for it in items])
    elif scheme == "vol_target":
        raw = np.array([_vol(it) for it in items])
    else:                                              # equal / 未知
        raw = np.ones(n)
    raw = np.nan_to_num(raw, nan=0.0)
    if raw.sum() <= 0:
        raw = np.ones(n)
    w = raw / raw.sum()
    for _ in range(10):                                # 單檔上限 + 重正規化（迭代收斂）
        over = w > max_w
        if not over.any():
            break
        excess = float((w[over] - max_w).sum())
        w[over] = max_w
        under = ~over
        pool = float(w[under].sum())
        if pool <= 0:
            break
        w[under] += excess * w[under] / pool
    s = float(w.sum())
    return (w / s).tolist() if s > 0 else (np.ones(n) / n).tolist()


def backtest_sizing(*, horizon: int | None = None,
                    costs_pct: tuple[float, ...] = (0.4, 0.6, 0.9, 1.2),
                    max_w: float | None = None) -> dict[str, Any]:
    """部位 sizing 的「淨 P&L」回測（純離線、不存模型、不動晨報）。

    OOS purged walk-forward：逐折用過去資料訓練 risk(risk_label)+meta(meta_label) 分類器→predict 未來
    risk_p/meta_p；每 OOS 日對偏多候選用各 sizing 方案算權重 → 組合 h 日毛報酬，對比等權基準。
    回答「把 risk×meta 分數拿來加權，扣成本後會不會贏等權」。mirror backtest_smallcap_sleeve。
    """
    import numpy as np  # noqa: PLC0415

    from reports import training_set as T  # noqa: PLC0415

    h = horizon or backtest.horizons()[0]
    max_w = settings.sizing_max_weight if max_w is None else max_w
    big = T._build_big([h])
    if big is None or big.empty:
        return {"ran": False, "reason": "no usable symbols"}
    sc = T._emit_all(big, [h], min_amount=T.MIN_AMOUNT_TWD, max_amount=None)
    df = sc[(sc["horizon"] == h) & (sc["side"] == "watchlist") & sc["fwd_return_pct"].notna()
            & sc["meta_label"].notna() & sc["risk_label"].notna()].copy()
    df = df.sort_values("as_of").reset_index(drop=True)
    if len(df) < 500:
        return {"ran": False, "reason": f"too few rows {len(df)}"}

    dates = sorted(df["as_of"].unique().tolist())
    nd = len(dates)
    pos = {d: i for i, d in enumerate(dates)}
    dpos = df["as_of"].map(pos).to_numpy()
    wv = df["weight"].astype(float).to_numpy() if "weight" in df.columns else None
    feats = {"meta": (df[META_FEATURE_COLUMNS].astype(float), df["meta_label"].astype(int).to_numpy()),
             "risk": (df[FEATURE_COLUMNS].astype(float), df["risk_label"].astype(int).to_numpy())}
    meta_p = np.full(len(df), np.nan)
    risk_p = np.full(len(df), np.nan)

    K = 5
    edges = [round(nd * k / K) for k in range(K + 1)]
    for k in range(1, K):
        ts, te = edges[k], edges[k + 1]
        if te <= ts:
            continue
        tr = dpos < (ts - h)                           # embargo h 天
        tem = (dpos >= ts) & (dpos < te)
        if tr.sum() < 50 or tem.sum() < 5:
            continue
        for name, (X, y) in feats.items():
            if len(set(y[tr])) < 2:
                continue
            cols = [c for c in X.columns if X[c][tr].nunique(dropna=True) >= 2]
            if not cols:
                continue
            m = _new_model()
            m.fit(X[cols][tr], y[tr], sample_weight=(wv[tr] if wv is not None else None))
            p = m.predict_proba(X[cols][tem])[:, 1]
            (meta_p if name == "meta" else risk_p)[np.where(tem)[0]] = p

    df["meta_p"], df["risk_p"] = meta_p, risk_p
    oos = df[df["meta_p"].notna() & df["risk_p"].notna()]
    daily: dict[str, list[float]] = {s: [] for s in _SIZING_SCHEMES}
    n_per_day: list[int] = []
    for _d, g in oos.groupby("as_of"):
        if len(g) < 2:
            continue
        items = [{"meta_p": r.meta_p, "risk_p": r.risk_p, "vol": r.volatility_20d_pct}
                 for r in g.itertuples()]
        fwd = g["fwd_return_pct"].to_numpy(dtype=float)
        n_per_day.append(len(g))
        for s in _SIZING_SCHEMES:
            w = np.array(_size_weights(items, s, max_w=max_w))
            daily[s].append(float((w * fwd).sum()))
    if len(n_per_day) < 20:
        return {"ran": False, "reason": f"too few oos days {len(n_per_day)}"}

    eq = np.array(daily["equal"])
    res: dict[str, Any] = {}
    for s in _SIZING_SCHEMES:
        arr = np.array(daily[s])
        gross = float(arr.mean())
        sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else None
        res[s] = {
            "gross_mean_pct": round(gross, 3),
            "alpha_vs_equal_pct": round(float((arr - eq).mean()), 4),
            "sharpe_per_period": round(sharpe, 3) if sharpe is not None else None,
            "hit_rate_vs_equal": round(float((arr > eq).mean()), 3),
            "net_pct": {f"cost{c}": round(gross - c, 3) for c in costs_pct},
        }
    cand = {s: res[s]["alpha_vs_equal_pct"] for s in _SIZING_SCHEMES if s != "equal"}
    best = max(cand, key=cand.get)
    best_scheme = best if cand[best] > settings.sizing_min_alpha else None
    out = {
        "ran": True, "horizon": h, "oos_days": len(n_per_day),
        "avg_candidates_per_day": round(float(np.mean(n_per_day)), 1),
        "max_weight": max_w, "schemes": res, "best_scheme": best_scheme,
        "note": ("alpha_vs_equal＝毛報酬−等權(同階成本相抵的頭條)；sharpe＝逐 h 日報酬 mean/std。"
                 "best_scheme 須 alpha>sizing_min_alpha 才回(否則 None＝退回等權)。long-only 偏多書。"),
        "generated_at": datetime.now(ZoneInfo(settings.tz)).isoformat(timespec="seconds"),
    }
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    SIZING_BACKTEST_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("sizing 回測：best_scheme=%s（各方案 alpha_vs_equal=%s）",
                best_scheme, {s: res[s]["alpha_vs_equal_pct"] for s in _SIZING_SCHEMES})
    return out


def sizing_plan() -> str | None:
    """serving 讀 gate：回最佳 sizing 方案名（回測證明淨贏等權才回），否則 None＝退回等權。"""
    if not settings.enable_sizing:
        return None
    bt = _load_json(SIZING_BACKTEST_PATH) or {}
    return bt.get("best_scheme") or None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except Exception:  # noqa: BLE001
        return None
