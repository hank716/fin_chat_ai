"""WP3.1：gemini_client 重試裝飾器修正。

驗證：(a) _generate_json 現在有重試（503×3 後成功、共 4 次呼叫）；(b) 429 fail-fast 只 1 次；
(c) generate_text 只剩單層重試（503 恰好 4 次，非疊層的 12 次）；(d) _usage_of 不再掛 retry。
mock httpx.post，並把 tenacity 的 sleep 換成 no-op 以免真的等待。
"""
from __future__ import annotations

import httpx
import pytest

from ai import gemini_client as gc


class _Resp:
    def __init__(self, status: int, body: dict | None = None, text: str = ""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


_OK_BODY = {
    "candidates": [{"content": {"parts": [{"text": "{\"ok\": 1}"}]}}],
    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
}
_OK_TEXT_BODY = {
    "candidates": [{"content": {"parts": [{"text": "hello"}]}}],
    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
}


@pytest.fixture(autouse=True)
def _fast_and_keyed(monkeypatch):
    monkeypatch.setattr(gc.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(gc.monitor, "mark", lambda *a, **k: None)
    # 關掉 tenacity 真實等待（否則 exponential backoff 會讓測試 sleep 數秒）
    gc._generate_json.retry.sleep = lambda *_: None
    gc.generate_text.retry.sleep = lambda *_: None


def _sequence_post(statuses, body):
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        st = statuses[min(i, len(statuses) - 1)]
        return _Resp(st, body if st == 200 else None, text="err")

    return fake_post, calls


def test_generate_json_retries_503_then_succeeds(monkeypatch):
    fake, calls = _sequence_post([503, 503, 503, 200], _OK_BODY)
    monkeypatch.setattr(gc.httpx, "post", fake)
    out, usage = gc._generate_json("p", {"type": "object"})
    assert out == {"ok": 1}
    assert calls["n"] == 4                        # 3 次 503 + 第 4 次成功


def test_generate_json_503_exhausts_at_four(monkeypatch):
    fake, calls = _sequence_post([503], None)
    monkeypatch.setattr(gc.httpx, "post", fake)
    with pytest.raises(gc.GeminiUnavailable):
        gc._generate_json("p", {"type": "object"})
    assert calls["n"] == 4                         # stop_after_attempt(4)


def test_generate_json_429_fails_fast(monkeypatch):
    fake, calls = _sequence_post([429], None)
    monkeypatch.setattr(gc.httpx, "post", fake)
    with pytest.raises(gc.GeminiQuotaExceeded):
        gc._generate_json("p", {"type": "object"})
    assert calls["n"] == 1                         # 配額用盡不重試


def test_generate_text_single_retry_layer(monkeypatch):
    """generate_text 對 503 恰好嘗試 4 次（若還是舊的疊兩層 retry，會變 3×4=12 次）。"""
    fake, calls = _sequence_post([503], None)
    monkeypatch.setattr(gc.httpx, "post", fake)
    with pytest.raises(gc.GeminiUnavailable):
        gc.generate_text("p")
    assert calls["n"] == 4


def test_generate_text_succeeds_after_retry(monkeypatch):
    fake, calls = _sequence_post([503, 200], _OK_TEXT_BODY)
    monkeypatch.setattr(gc.httpx, "post", fake)
    text, usage = gc.generate_text("p")
    assert text == "hello"
    assert calls["n"] == 2


def test_usage_of_is_not_retry_wrapped():
    """純資料整理函式不該掛 retry（tenacity 會在被裝飾的函式掛 .retry 屬性）。"""
    assert not hasattr(gc._usage_of, "retry")
    # 而真正發 HTTP 的兩個函式應有 retry
    assert hasattr(gc._generate_json, "retry")
    assert hasattr(gc.generate_text, "retry")
