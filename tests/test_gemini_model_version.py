"""`*-latest` 別名換代偵測：實際 modelVersion 要留痕跡，換代與牌價不符都要 warn。"""
from __future__ import annotations

import logging

import pytest

import redis_client as rc
from ai import gemini_client as gc


class _FakeRedis:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


@pytest.fixture(autouse=True)
def _clean():
    gc._resolved_versions.clear()
    yield
    gc._resolved_versions.clear()


def test_resolved_version_recorded(monkeypatch, caplog):
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "redis_client", fake)
    with caplog.at_level(logging.WARNING):
        gc._note_model_version("gemini-flash-latest", {"modelVersion": "gemini-3.8-flash"})
    assert gc.resolved_version("gemini-flash-latest") == "gemini-3.8-flash"
    assert fake.data["gemini:model_version:gemini-flash-latest"] == "gemini-3.8-flash"
    assert not caplog.records            # 與牌價核對版本一致、無前值 → 不吵


def test_alias_rollover_warns_and_flags_pricing(monkeypatch, caplog):
    fake = _FakeRedis({"gemini:model_version:gemini-flash-latest": "gemini-3.8-flash"})
    monkeypatch.setattr(rc, "redis_client", fake)
    with caplog.at_level(logging.WARNING):
        gc._note_model_version("gemini-flash-latest", {"modelVersion": "gemini-4.0-flash"})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "已換代" in text and "牌價" in text


def test_redis_failure_never_breaks_the_call(monkeypatch):
    class _Boom:
        def get(self, key):
            raise RuntimeError("redis down")

    monkeypatch.setattr(rc, "redis_client", _Boom())
    gc._note_model_version("gemini-pro-latest", {"modelVersion": "gemini-3.1-pro-preview"})
    assert gc.resolved_version("gemini-pro-latest") == "gemini-3.1-pro-preview"
    gc._note_model_version("gemini-pro-latest", {})       # 缺欄位也不炸
