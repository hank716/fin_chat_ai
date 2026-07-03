# Implementation Plan: 儲存管理 — parquet SSOT、10GB 預算、retention、pCloud restore

**Branch**: `002-storage-manager` | **Date**: 2026-07-03 | **Spec**: [spec.md](./spec.md)

**Note**: Baseline 回溯計畫——記錄已採用的技術決策（非新設計）。

## Summary

本機 per-symbol parquet 為 SSOT，依 trade_date upsert 並在寫入前擋未來日（防幽靈列汙染 as_of）；
storage_monitor 以 st_blocks 計 footprint vs `LOCAL_STORAGE_BUDGET_GB` 與主機磁碟雙視角警示；
retention 清舊報告/adhoc parquet（可回補故可安全清）；pCloud 冷備份 + on-demand restore。

## Technical Context

**Language/Version**: Python 3.13
**Primary Dependencies**: pandas、pyarrow；`shutil`（disk_usage）；httpx（pCloud）
**Storage**: 本機 `storage/`（parquet SSOT + reports）；pCloud 冷儲存
**Testing**: pytest（**目前缺，列為待辦**）
**Target Platform**: Linux server（backend 容器）
**Project Type**: web-service 儲存層
**Performance Goals**: footprint 掃描 O(檔案數)；每交易日增量 heuristic ~80MB
**Constraints**: local-first ≤ 10GB；未來日不得落地；監控/清理/備份失敗不阻斷主流程
**Scale/Scope**: `backend/storage/*.py`（local_store 169 / storage_monitor 116 / retention 71）+ pcloud_backup

## Constitution Check

*GATE: 對照憲章七原則。*

- **V. Local-First Storage** — ✅ parquet SSOT + `LOCAL_STORAGE_BUDGET_GB`（10GB）+ retention + pCloud 冷儲存/回補。
- **IV. Point-in-Time** — ✅ 未來日寫入閘（`_FUTURE_GRACE_DAYS`）為跨來源最後一道閘（commit `ec8c403`）。
- **VI. 服務隔離（延伸）** — ✅ 儲存層供 backend 使用；pCloud/Supabase 發布為選用旁路。
- **VII. Spec-Driven / Conventional Commits** — ✅ 本基線規格補齊。
- I/II/III — N/A（非 LLM/成本/輸出）。

**穩健性設計（非違規）**：監控/清理/備份失敗只記 log。**結論**：通過。

## Project Structure

### Documentation (this feature)

```text
specs/002-storage-manager/
├── spec.md
├── plan.md      # 本檔
└── tasks.md
```

### Source Code (repository root)

```text
backend/storage/
├── local_store.py       # write_prices/chip/margin（per-symbol upsert）+ 未來日寫入閘 _future_cutoff
├── storage_monitor.py   # local_storage_report（footprint st_blocks vs budget + host disk + alert）
└── retention.py         # enforce_retention（_evict_old_reports + _prune_adhoc_parquet）
backend/publish/pcloud_backup.py   # backup_report / restore_report（冷儲存，失敗只記 log）
configs / storage layout（design_docs §28）
tests/storage/           # ⬜ 待新增
```

**Structure Decision**: 落地/監控/清理三職責分檔；未來日閘放在最靠近寫入處（local_store）以擋所有來源；
pCloud 備份/回補獨立於 publish，失敗不影響核心落地。

## Complexity Tracking

> 無 Constitution 違規，免填。
