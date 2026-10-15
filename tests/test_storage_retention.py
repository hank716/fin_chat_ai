"""spec 002 測試基線：寫入閘擋未來日、容量告警分級、retention 汰換、pCloud 回補與失敗不中斷。

全離線：磁碟用量、pCloud HTTP 都換成替身；檔案都在 tmp_path。
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from publish import pcloud_backup
from storage import local_store, retention, storage_monitor


# ── T010 trade_date 超過今日+寬限 → 寫入時就丟掉 ──

def test_write_gate_drops_future_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store, "PARQUET_ROOT", tmp_path)
    today = date.today()
    from data_sources.twse_loader import PriceRow
    row = lambda d: PriceRow(symbol="2330", trade_date=d, open=1, high=1, low=1,  # noqa: E731
                             close=1, volume=1, amount=1, source="twse")
    local_store.write_prices([row(today), row(today + timedelta(days=2)),
                              row(today + timedelta(days=3)), row(today + timedelta(days=30))], "tw")
    days = set(pd.to_datetime(local_store.read_prices("2330", "tw")["trade_date"]).dt.date)
    assert days == {today, today + timedelta(days=2)}       # 寬限 2 天內保留，之後全丟


# ── T012 容量報告：footprint≥budget→critical、主機 free<15GB→critical、整體取最嚴重 ──

GB = 1024 ** 3


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_monitor, "STORAGE_ROOT", tmp_path)
    state = {"used": 0, "free": 100 * GB}
    monkeypatch.setattr(storage_monitor, "_dir_size",
                        lambda p: state["used"] if p.name == "local_parquet" else 0)
    monkeypatch.setattr(storage_monitor.shutil, "disk_usage",
                        lambda _p: SimpleNamespace(total=500 * GB, used=500 * GB - state["free"],
                                                   free=state["free"]))
    monkeypatch.setattr(storage_monitor.settings, "local_storage_budget_gb", 10)
    return state


@pytest.mark.parametrize("used_gb,free_gb,footprint,host,overall", [
    (5, 100, "ok", "ok", "ok"),
    (7, 100, "warning", "ok", "warning"),             # 70% 預算
    (10, 100, "critical", "ok", "critical"),          # 達預算
    (1, 20, "ok", "warning", "warning"),              # 主機 < 30GB
    (1, 10, "ok", "critical", "critical"),            # 主機 < 15GB
    (7, 10, "warning", "critical", "critical"),       # 取最嚴重
])
def test_storage_alert_levels(monitor, used_gb, free_gb, footprint, host, overall):
    monitor["used"], monitor["free"] = used_gb * GB, free_gb * GB
    rep = storage_monitor.local_storage_report()
    assert (rep["footprint_alert_level"], rep["host_alert_level"], rep["alert_level"]) == \
        (footprint, host, overall)


# ── T013 retention：汰換最舊報告（json+md）、保留全市場標的、清掉過期的清單外快取 ──

@pytest.fixture
def store(tmp_path, monkeypatch):
    reports, parquet = tmp_path / "reports", tmp_path / "local_parquet"
    reports.mkdir()
    (parquet / "tw" / "_chip").mkdir(parents=True)
    monkeypatch.setattr(retention, "REPORTS_DIR", reports)
    monkeypatch.setattr(retention, "PARQUET_ROOT", parquet)
    monkeypatch.setattr(retention, "KEEP_REPORTS_LOCAL", 3)
    monkeypatch.setattr(retention.universe, "watchlist_symbols", lambda: frozenset({"2330"}))
    monkeypatch.setattr(retention, "local_storage_report",
                        lambda: {"used_gb": 1.0, "budget_gb": 10.0, "alert_level": "ok"})
    return reports, parquet


def _touch(path, age_days=0):
    path.write_text("x")
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))


def test_retention_evicts_oldest_reports_json_and_md(store):
    reports, _ = store
    ids = [f"morning_202610{d:02d}_082000" for d in range(1, 6)]
    for rid in ids:
        _touch(reports / f"{rid}.json")
        _touch(reports / f"{rid}.md")
    out = retention.enforce_retention()
    assert out["reports_evicted"] == 2
    assert sorted(p.stem for p in reports.glob("*.json")) == ids[2:]
    assert sorted(p.stem for p in reports.glob("*.md")) == ids[2:]


def test_retention_keeps_universe_and_index_prunes_stale_adhoc(store):
    _, parquet = store
    tw = parquet / "tw"
    _touch(tw / "2330.parquet", age_days=60)              # 全市場標的：再舊也留
    _touch(tw / "TWII.parquet", age_days=60)              # 大盤：留
    _touch(tw / "9999.parquet", age_days=30)              # 清單外、過期：清
    _touch(tw / "8888.parquet", age_days=1)               # 清單外、還新：留
    _touch(tw / "_chip" / "9999.parquet", age_days=30)
    out = retention.enforce_retention()
    assert out["adhoc_parquet_pruned"] == 2
    assert sorted(p.name for p in tw.glob("*.parquet")) == ["2330.parquet", "8888.parquet", "TWII.parquet"]


# ── T014 restore_report 從 pCloud 下載回本機 ──

def test_restore_report_downloads_json_and_md(tmp_path, monkeypatch):
    monkeypatch.setattr(pcloud_backup.settings, "pcloud_access_token", "tok")
    monkeypatch.setattr(pcloud_backup.settings, "local_storage_path", str(tmp_path))
    got = []

    def download(remote, dest):
        got.append(remote.rsplit("/", 1)[-1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{}")
        return True

    monkeypatch.setattr(pcloud_backup, "_download", download)
    assert pcloud_backup.restore_report("morning_20260101_082000") is True
    assert got == ["morning_20260101_082000.json", "morning_20260101_082000.md"]
    assert (tmp_path / "reports" / "morning_20260101_082000.json").exists()


def test_restore_without_token_does_nothing(monkeypatch):
    monkeypatch.setattr(pcloud_backup.settings, "pcloud_access_token", " ")
    monkeypatch.setattr(pcloud_backup, "_download", lambda *a: pytest.fail("不該下載"))
    assert pcloud_backup.restore_report("morning_20260101_082000") is False


# ── T015 pCloud / retention 失敗不拋、不阻斷 ──

def test_pcloud_download_failure_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(pcloud_backup.settings, "pcloud_access_token", "tok")

    def boom(*_a, **_k):
        raise ConnectionError("pcloud down")

    monkeypatch.setattr(pcloud_backup.httpx, "get", boom)
    assert pcloud_backup._download("/x.json", tmp_path / "x.json") is False
    assert not (tmp_path / "x.json").exists()


def test_backup_without_token_or_folder_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(pcloud_backup.settings, "pcloud_access_token", "")
    assert pcloud_backup.backup_report(tmp_path / "a.json", tmp_path / "a.md") == {}
    monkeypatch.setattr(pcloud_backup.settings, "pcloud_access_token", "tok")
    monkeypatch.setattr(pcloud_backup, "_ensure_folder", lambda: None)
    assert pcloud_backup.backup_report(tmp_path / "a.json", tmp_path / "a.md") == {}


def test_retention_tolerates_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, "REPORTS_DIR", tmp_path / "none")
    monkeypatch.setattr(retention, "PARQUET_ROOT", tmp_path / "none")
    monkeypatch.setattr(retention, "local_storage_report",
                        lambda: {"used_gb": 0, "budget_gb": 1, "alert_level": "ok"})
    out = retention.enforce_retention()
    assert (out["reports_evicted"], out["adhoc_parquet_pruned"]) == (0, 0)

