"""WP3.4：prompt features 預算截斷 + 格式化段降溫。"""
from __future__ import annotations

import httpx
import pytest

from ai import gemini_client as gc
from ai import prompts


def _make_features(n_stocks: int, n_news: int, protected: list[str]):
    stocks = {f"S{i:04d}": {"amount": float(i), "name": f"n{i}"} for i in range(n_stocks)}
    for p in protected:                       # protected 給小 amount，證明是「因 movers 被保留」而非因量大
        stocks[p] = {"amount": 1.0, "name": p}
    movers = {"top_gainers_5d": [{"symbol": p} for p in protected]}
    news = [{"symbol": "X", "title": "長" * 500, "url": "http://e/x", "tier": "authoritative"}
            for _ in range(n_news)]
    return {"tw": {"stocks": stocks, "movers": movers}, "news": news}


def test_budget_caps_stocks_but_keeps_movers_symbols():
    """stocks 超過上限時砍到 MAX_BRIEF_STOCKS，但 movers 涉及標的（即使 amount 小）必留。"""
    protected = ["2330", "2317", "2454"]
    feats = _make_features(n_stocks=prompts.MAX_BRIEF_STOCKS * 3, n_news=5, protected=protected)
    out = prompts._budget_features(feats)
    kept = out["tw"]["stocks"]
    assert len(kept) == prompts.MAX_BRIEF_STOCKS
    for p in protected:                       # movers 候選未被截
        assert p in kept


def test_budget_truncates_news_text_and_count():
    feats = _make_features(n_stocks=10, n_news=prompts.MAX_BRIEF_NEWS * 2, protected=["2330"])
    out = prompts._budget_features(feats)
    assert len(out["news"]) == prompts.MAX_BRIEF_NEWS
    for n in out["news"]:
        assert len(n["title"]) <= prompts.NEWS_TEXT_MAX + 1   # +1 為截斷符「…」
        assert n["url"] == "http://e/x"                       # url 不截斷


def test_budget_is_pure_does_not_mutate_input():
    feats = _make_features(n_stocks=5, n_news=3, protected=["2330"])
    before_stocks = dict(feats["tw"]["stocks"])
    before_news = len(feats["news"])
    prompts._budget_features(feats)
    assert feats["tw"]["stocks"] == before_stocks
    assert len(feats["news"]) == before_news


def test_small_features_pass_through_stocks_unchanged():
    feats = _make_features(n_stocks=10, n_news=2, protected=["2330"])
    out = prompts._budget_features(feats)
    assert len(out["tw"]["stocks"]) == 11         # 10 + 1 protected，未觸發 cap


# ── 格式化段降溫：_generate_json 溫度可參數化，結構化呼叫用 0.1 ──

class _Resp:
    status_code = 200

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": "{\"ok\":1}"}]}}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}


def test_generate_json_temperature_is_parametrized(monkeypatch):
    monkeypatch.setattr(gc.settings, "gemini_api_key", "k")
    monkeypatch.setattr(gc.monitor, "mark", lambda *a, **k: None)
    captured = {}

    def fake_post(url, **kwargs):
        captured["payload"] = kwargs.get("json")
        return _Resp()

    monkeypatch.setattr(gc.httpx, "post", fake_post)
    gc._generate_json("p", {"type": "object"}, temperature=0.1)
    assert captured["payload"]["generationConfig"]["temperature"] == 0.1
    # 預設仍為 0.4
    gc._generate_json("p", {"type": "object"})
    assert captured["payload"]["generationConfig"]["temperature"] == 0.4
