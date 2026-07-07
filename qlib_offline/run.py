"""Qlib 離線評估/打分 entrypoint（階段 3 [8]）。

用 Qlib Alpha158 因子庫（158 個 OHLCV 衍生因子）＋ LightGBM 做時間序 purged walk-forward：
  方向：逐日橫斷面 rank-IC / ICIR（對齊 backend strategy_calibration._evaluate_rank_oos 的定義）
  風險：未來 h 日 MAE 橫斷面中位數切分 → OOS AUC（對齊 training_set._emit_samples 的 risk_label）

模式：
  --eval   只評估，落地 storage/strategy/qlib_eval.json（核心問題：Alpha158 能否破 0.027 方向牆）。
  --score  訓練 → 替最近交易日的流動性股池打分，落地 qlib_scores/{date}.json + qlib_meta.json（給 serving 比 gate）。
  --loop   啟動跑一次 eval+score，之後每日 off-peak（03:00 台北）重跑（容器自帶排程，不需 scheduler）。

純離線、讀本機 parquet、零外部 API、零 LLM。serving(backend) 只讀 JSON，永不 import qlib。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from common import (
    EVAL_PATH,
    META_PATH,
    MIN_AMOUNT_TWD,
    QLIB_DATA,
    SCORES_DIR,
    available_symbols,
    horizons,
    log,
    write_json,
)

_TZ = "Asia/Taipei"
_NIGHTLY_HOUR = 3
_RANK_GATE = 0.03            # 與 backend rank_ic_gate 對齊（僅供 eval JSON 標註「破牆與否」）


# ───────────────────────── 資料載入 ─────────────────────────

def _init_qlib() -> None:
    import qlib  # noqa: PLC0415
    qlib.init(provider_uri=str(QLIB_DATA), region="cn",
              expression_cache=None, dataset_cache=None, redis_port=-1)


def _load_features_labels(syms: list[str], hs: list[int]) -> tuple[pd.DataFrame, dict]:
    """回 (df, info)：df index=(datetime,instrument)，含 Alpha158 特徵 + amount + 每窗 fwd_ret/fwd_mae。"""
    from qlib.contrib.data.handler import Alpha158  # noqa: PLC0415
    from qlib.data import D  # noqa: PLC0415
    from qlib.data.dataset.handler import DataHandlerLP  # noqa: PLC0415

    cal = D.calendar()
    start, end = cal[0], cal[-1]
    log.info("Alpha158 特徵計算中（%d 檔，%s ~ %s）…", len(syms), start, end)
    h = Alpha158(instruments=syms, start_time=start, end_time=end,
                 fit_start_time=start, fit_end_time=end,
                 infer_processors=[], learn_processors=[])    # 空 processors＝取原始因子（NaN 由 lgbm 原生吃）
    feat = h.fetch(col_set="feature", data_key=DataHandlerLP.DK_I)
    feat_cols = list(feat.columns)
    log.info("Alpha158 特徵：%d 欄 / %d 列", len(feat_cols), len(feat))

    # 標籤 + 流動性（amount=$vwap*$volume）。逐窗 fwd_ret / fwd_mae（未來，無洩漏）。
    exprs = ["$vwap*$volume"]
    names = ["amount"]
    for hh in hs:
        exprs += [f"Ref($close,-{hh})/$close-1", f"Ref(Min($low,{hh}),-{hh})/$close-1"]
        names += [f"fwd_ret_{hh}", f"fwd_mae_{hh}"]
    lab = D.features(syms, exprs, start_time=start, end_time=end)
    lab.columns = names
    lab = lab.swaplevel().sort_index()                       # (instrument,datetime) → (datetime,instrument)

    df = feat.join(lab, how="inner")
    df.index = df.index.set_names(["datetime", "instrument"])
    return df, {"feature_cols": feat_cols, "date_range": [str(start), str(end)]}


# ───────────────────────── walk-forward 評估 ─────────────────────────

def _walk_forward(df: pd.DataFrame, feat_cols: list[str], target: str, h: int, *,
                  classifier: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """K=5 purged walk-forward + embargo h：回 (oos_pred, oos_target, oos_datepos, folds)。

    與 backend strategy_calibration 同切分：唯一日期切 K 段、逐段當 test、其前樣本當 train、
    丟掉 test 起始前 h 個交易日的 train（前瞻窗重疊→洩漏）。
    """
    import lightgbm as lgb  # noqa: PLC0415

    sub = df[df[target].notna()].copy()
    if sub.empty:
        return np.array([]), np.array([]), np.array([]), 0
    dates = sorted(sub.index.get_level_values("datetime").unique())
    pos = {d: i for i, d in enumerate(dates)}
    dpos = sub.index.get_level_values("datetime").map(pos).to_numpy()
    X = sub[feat_cols].to_numpy(dtype="float32")
    y = sub[target].to_numpy(dtype="float64")
    nd = len(dates)
    K = 5
    oos_p: list[np.ndarray] = []
    oos_t: list[np.ndarray] = []
    oos_d: list[np.ndarray] = []
    folds = 0
    if nd < 2 * K:
        return np.array([]), np.array([]), np.array([]), 0
    edges = [round(nd * k / K) for k in range(K + 1)]
    for k in range(1, K):
        ts, te = edges[k], edges[k + 1]
        if te <= ts:
            continue
        tr = dpos < (ts - h)
        tem = (dpos >= ts) & (dpos < te)
        if tr.sum() < 200 or tem.sum() < 20:
            continue
        ytr = y[tr]
        if classifier and len(np.unique(ytr)) < 2:
            continue
        params = dict(n_estimators=200, learning_rate=0.05, num_leaves=31,
                      max_depth=6, subsample=0.8, colsample_bytree=0.8,
                      min_child_samples=50, n_jobs=-1, verbosity=-1, random_state=42)
        model = lgb.LGBMClassifier(**params) if classifier else lgb.LGBMRegressor(**params)
        model.fit(X[tr], ytr)
        p = (model.predict_proba(X[tem])[:, 1] if classifier else model.predict(X[tem]))
        oos_p.append(np.asarray(p))
        oos_t.append(y[tem])
        oos_d.append(dpos[tem])
        folds += 1
    if folds == 0:
        return np.array([]), np.array([]), np.array([]), 0
    return (np.concatenate(oos_p), np.concatenate(oos_t),
            np.concatenate(oos_d), folds)


def _rank_metrics(pred: np.ndarray, tgt: np.ndarray, dpos: np.ndarray) -> dict:
    """逐日橫斷面 Spearman 平均＝rank_ic；ICIR＝逐日 IC 的 mean/std（對齊 backend 定義）。"""
    from scipy.stats import spearmanr  # noqa: PLC0415

    if pred.size == 0:
        return {"rank_ic": None, "icir": None, "n_days": 0}
    daily: list[float] = []
    for d in np.unique(dpos):
        m = dpos == d
        if m.sum() >= 5 and len(np.unique(pred[m])) > 1 and len(np.unique(tgt[m])) > 1:
            ic, _ = spearmanr(pred[m], tgt[m])
            if ic == ic:
                daily.append(float(ic))
    if not daily:
        return {"rank_ic": None, "icir": None, "n_days": 0}
    arr = np.asarray(daily)
    return {
        "rank_ic": round(float(arr.mean()), 4),
        "icir": round(float(arr.mean() / arr.std()), 3) if arr.std() > 0 else None,
        "n_days": len(daily),
    }


def _risk_label(df: pd.DataFrame, h: int) -> pd.Series:
    """未來 h 日 MAE 的逐日橫斷面中位數切分：跌更深(<中位數)＝高風險正類（對齊 training_set）。"""
    col = f"fwd_mae_{h}"
    med = df.groupby(level="datetime")[col].transform("median")
    lab = (df[col] < med).astype("float64")
    lab[df[col].isna()] = np.nan
    return lab


def _liquid(df: pd.DataFrame) -> pd.DataFrame:
    """主池流動性過濾：當日 amount >= MIN_AMOUNT_TWD（對齊 training_set；ETF 已在 universe 階段排除）。"""
    return df[df["amount"].fillna(0) >= MIN_AMOUNT_TWD]


# ───────────────────────── eval / score ─────────────────────────

def _eval_per_horizon(liquid: pd.DataFrame, fc: list[str], hs: list[int],
                      syms: list[str], info: dict) -> dict:
    """純運算：對已載入的 liquid 算各窗 rank-IC/risk-AUC，回 eval out dict（不做 IO load）。"""
    per: dict[str, dict] = {}
    for h in hs:
        log.info("── 評估窗 h=%d ──", h)
        # 方向 rank-IC：target=未來報酬，逐日橫斷面 Spearman。
        p, t, d, folds = _walk_forward(liquid, fc, f"fwd_ret_{h}", h, classifier=False)
        rk = _rank_metrics(p, t, d)
        # 風險 AUC：target=MAE 中位數切分。
        ld = liquid.copy()
        ld[f"risk_{h}"] = _risk_label(ld, h)
        rp, rt, _rd, rfolds = _walk_forward(ld, fc, f"risk_{h}", h, classifier=True)
        risk_auc = None
        if rp.size and len(np.unique(rt)) == 2:
            from sklearn.metrics import roc_auc_score  # noqa: PLC0415
            risk_auc = round(float(roc_auc_score(rt, rp)), 3)
        per[str(h)] = {
            "rank_ic": rk["rank_ic"], "icir": rk["icir"], "ic_n_days": rk["n_days"],
            "rank_folds": folds, "risk_auc": risk_auc, "risk_folds": rfolds,
            "rank_passes_gate": bool(rk["rank_ic"] is not None and rk["rank_ic"] >= _RANK_GATE),
        }
        log.info("h=%d：rank_ic=%s icir=%s（gate %.2f）/ risk_auc=%s",
                 h, rk["rank_ic"], rk["icir"], _RANK_GATE, risk_auc)

    any_break = any(v["rank_passes_gate"] for v in per.values())
    return {
        "ran": True,
        "generated_at": datetime.now(ZoneInfo(_TZ)).isoformat(timespec="seconds"),
        "method": "alpha158 + lightgbm, purged walk-forward (K=5, embargo=h)",
        "symbols": len(syms),
        "feature_count": len(fc),
        "date_range": info["date_range"],
        "horizons": hs,
        "price_basis": "adjusted",   # spec 017 / WP2.1：dump 已做除權息還原（消除除息跳空）
        "per_horizon": per,
        "breaks_direction_wall": any_break,
        "note": ("rank-IC＝流動性全池(amount>=50M)逐日橫斷面 Spearman，非 sector-neutral 殘差，"
                 "故與 backend 殘差 rank-IC 0.027 比為『偏寬鬆』基準：若此值仍<0.03＝方向牆確認；"
                 "risk_auc 與 backend 風險模型 ~0.69 同定義(MAE 中位數切分)可直接比。"),
    }


def _load_liquid(hs: list[int]):
    """共用載入：init qlib → Alpha158 特徵+標籤 → 流動性過濾。回 (liquid, fc, syms, info) 或 None。"""
    syms = available_symbols()
    if not syms:
        return None
    _init_qlib()
    df, info = _load_features_labels(syms, hs)
    fc = info["feature_cols"]
    liquid = _liquid(df)
    log.info("流動性過濾：%d → %d 列（amount>=%.0f）", len(df), len(liquid), MIN_AMOUNT_TWD)
    return liquid, fc, syms, info


def evaluate() -> dict:
    """只評估：載入一次 → 算各窗指標 → 寫 qlib_eval.json。"""
    hs = horizons()
    loaded = _load_liquid(hs)
    if loaded is None:
        return {"ran": False, "reason": "no symbols"}
    liquid, fc, syms, info = loaded
    out = _eval_per_horizon(liquid, fc, hs, syms, info)
    write_json(EVAL_PATH, out)
    log.info("qlib_eval.json 落地：breaks_wall=%s", out.get("breaks_direction_wall"))
    return out


def _train_and_score(liquid: pd.DataFrame, fc: list[str], hs: list[int], per_meta: dict) -> dict:
    """訓練主窗方向回歸(全史) → 替最新交易日橫斷面打分 → 寫 qlib_scores/{date}.json + qlib_meta.json。"""
    import lightgbm as lgb  # noqa: PLC0415

    ph = hs[0]
    train = liquid[liquid[f"fwd_ret_{ph}"].notna()]
    params = dict(n_estimators=300, learning_rate=0.05, num_leaves=31, max_depth=6,
                  subsample=0.8, colsample_bytree=0.8, min_child_samples=50,
                  n_jobs=-1, verbosity=-1, random_state=42)
    model = lgb.LGBMRegressor(**params)
    model.fit(train[fc].to_numpy(dtype="float32"), train[f"fwd_ret_{ph}"].to_numpy())

    latest_date = liquid.index.get_level_values("datetime").max()
    latest = liquid.xs(latest_date, level="datetime")          # 最新交易日（只需特徵，無未來標籤亦可打分）
    preds = model.predict(latest[fc].to_numpy(dtype="float32"))
    scores = {str(sym): round(float(p), 6) for sym, p in zip(latest.index.tolist(), preds)}

    date_str = pd.Timestamp(latest_date).strftime("%Y-%m-%d")
    write_json(SCORES_DIR / f"{date_str}.json",
               {"as_of": date_str, "primary_horizon": ph, "scores": scores})
    meta = {
        "trained_at": datetime.now(ZoneInfo(_TZ)).isoformat(timespec="seconds"),
        "score_family": "rank",          # qlib 服務方向排序（rank 家族），由 rank_ic_gate 守護
        "primary_horizon": ph,
        "horizons": hs,
        "latest_score_date": date_str,
        "per_horizon": {h: {"rank_ic": (per_meta.get(str(h), {}) or {}).get("rank_ic"),
                            "icir": (per_meta.get(str(h), {}) or {}).get("icir"),
                            "risk_auc": (per_meta.get(str(h), {}) or {}).get("risk_auc")}
                        for h in hs},
    }
    write_json(META_PATH, meta)
    log.info("qlib_scores/%s.json + qlib_meta.json 落地（%d 檔打分）", date_str, len(scores))
    return {"scored": True, "as_of": date_str, "n": len(scores)}


def score() -> dict:
    """評估＋打分（單次載入）：算各窗指標(供 meta 比 gate) → 訓練主窗 → 打分 → 寫 eval/scores/meta。"""
    hs = horizons()
    loaded = _load_liquid(hs)
    if loaded is None:
        return {"scored": False, "reason": "no symbols"}
    liquid, fc, syms, info = loaded
    ev = _eval_per_horizon(liquid, fc, hs, syms, info)     # 一次載入同時供 eval 與 meta
    write_json(EVAL_PATH, ev)
    log.info("qlib_eval.json 落地：breaks_wall=%s", ev.get("breaks_direction_wall"))
    return _train_and_score(liquid, fc, hs, ev.get("per_horizon", {}))


# ───────────────────────── loop ─────────────────────────

def _seconds_to_next_nightly() -> float:
    now = datetime.now(ZoneInfo(_TZ))
    nxt = now.replace(hour=_NIGHTLY_HOUR, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _run_once() -> None:
    from dump import dump  # noqa: PLC0415
    log.info("=== Qlib 離線批次開始 ===")
    st = dump()
    if not st.get("dumped"):
        log.warning("dump 失敗，略過本輪: %s", st)
        return
    try:
        score()      # score() 內含 evaluate()，一次寫齊 eval/scores/meta
    except Exception as exc:  # noqa: BLE001
        log.exception("評估/打分失敗: %s", exc)
    log.info("=== Qlib 離線批次結束 ===")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", action="store_true", help="只評估，寫 qlib_eval.json")
    ap.add_argument("--score", action="store_true", help="訓練+打分，寫 qlib_scores/meta")
    ap.add_argument("--loop", action="store_true", help="啟動跑一次，之後每日 03:00 台北重跑")
    args = ap.parse_args()

    if args.eval and not (args.score or args.loop):
        from dump import dump  # noqa: PLC0415
        dump()
        evaluate()
        return 0
    if args.score and not args.loop:
        from dump import dump  # noqa: PLC0415
        dump()
        score()
        return 0
    # 預設 / --loop：跑一次後 nightly。
    _run_once()
    while True:
        secs = _seconds_to_next_nightly()
        log.info("下次 Qlib 重跑於 %.1f 小時後（03:00 台北）", secs / 3600)
        time.sleep(secs)
        _run_once()


if __name__ == "__main__":
    sys.exit(main())
