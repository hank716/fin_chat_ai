"""WP1.4 survivorship bias 稽核（scratchpad 級，純離線讀既有 parquet + training_set）。

量化：parquet 有價的股票 vs 目前 universe 快照的差集，及差集在訓練集的樣本占比、fwd_return 分布。
差集(P−U)＝有歷史價但不在當前 universe＝多為近期下市/暫停/非標準代號，是可測的 survivorship 代理。
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

import universe
from config import settings

ROOT = os.path.join(settings.local_storage_path, "local_parquet", "tw")
TRAIN = os.path.join(settings.local_storage_path, "strategy", "training_set.parquet")
INDEX = "TWII"


def main() -> None:
    parquet_syms = {os.path.splitext(os.path.basename(p))[0]
                    for p in glob.glob(os.path.join(ROOT, "*.parquet"))} - {INDEX}
    uni = set(universe._load().get("symbols", {}).keys())

    p_minus_u = parquet_syms - uni      # 有價、不在當前 universe（近期下市/非標準）
    u_minus_p = uni - parquet_syms      # 在 universe、無價（尚未回補；非 survivorship）

    print("=== 集合規模 ===")
    print(f"parquet 有價股票數 : {len(parquet_syms)}")
    print(f"universe 快照股票數 : {len(uni)}")
    print(f"P−U（有價不在universe）: {len(p_minus_u)}  例：{sorted(p_minus_u)[:15]}")
    print(f"U−P（在universe無價）  : {len(u_minus_p)}  例：{sorted(u_minus_p)[:15]}")

    if not os.path.exists(TRAIN):
        print("\n(無 training_set.parquet，略過樣本占比)")
        return
    ds = pd.read_parquet(TRAIN)
    total = len(ds)
    in_diff = ds["symbol"].isin(p_minus_u)
    n_diff = int(in_diff.sum())
    share = n_diff / total if total else 0.0

    print("\n=== 訓練集樣本占比 ===")
    print(f"訓練集總樣本      : {total}")
    print(f"來自 P−U 的樣本   : {n_diff}  占比 {share:.4%}")

    # 也看「sector 查不到」的樣本占比（下市股 sector_of→None，plan 提到的 (b)(c)）
    no_sector = ds["symbol"].map(lambda s: universe.sector_of(s) is None)
    print(f"sector 查不到樣本 : {int(no_sector.sum())}  占比 {no_sector.mean():.4%}")

    print("\n=== fwd_return 分布：P−U vs 全體（各 horizon）===")
    for h in sorted(ds["horizon"].unique()):
        sub = ds[ds["horizon"] == h]
        col = "fwd_return_pct"
        allv = sub[col].dropna()
        dv = sub[sub["symbol"].isin(p_minus_u)][col].dropna()
        def stat(x):
            return (f"n={len(x)} mean={x.mean():.2f} med={x.median():.2f} "
                    f"std={x.std():.2f}") if len(x) else "n=0"
        print(f"h={h}: 全體 {stat(allv)}")
        print(f"      P−U  {stat(dv)}")

    print("\n=== 判定 ===")
    if share < 0.02:
        print(f"P−U 樣本占比 {share:.4%} < 2% → 記為已知限制、關閉（不開 spec 018）")
    else:
        print(f"P−U 樣本占比 {share:.4%} ≥ 2% → 建議開 spec 018-pit-universe（每日 universe 快照 + sector fallback）")


if __name__ == "__main__":
    main()
