"""問答精簡 context（2026-11-06）。

問答 system prompt 原本帶整包 features（實測 68k tokens），每輪工具迴圈都全價重送。
改成：清單只留摘要欄位；問題點名的個股另把完整區塊放進變動尾段。
"""
from __future__ import annotations

import json

import pytest

from ai import prompts
from api import ask
from cost import tracker as ct


def _stock(name: str) -> dict:
    return {
        "name": name, "sector": "半導體業", "as_of": "2026-11-05", "close": 100.0,
        "return_1d_pct": 1.0, "return_5d_pct": 2.0, "return_20d_pct": 3.0,
        "volatility_20d_pct": 2.5, "dist_ma20_pct": 4.0, "dist_ma60_pct": 5.0,
        "turnover_surge": 1.2, "vs_index_5d_pct": 0.5, "vs_index_20d_pct": 0.7,
        "foreign_net_buy_1d_lots": 10, "foreign_net_buy_5d_lots": 50, "foreign_net_streak": 3,
        "trust_net_streak": 1, "dealer_net_streak": 0, "margin_balance_lots": 999,
        "margin_chg_5d_lots": 12, "short_margin_ratio_pct": 8.0,
        "fundamentals": {"revenue_100m": 123.4, "eps_ttm": 9.9},
    }


@pytest.fixture
def report() -> dict:
    return {
        "markdown": "# 晨報",
        "features": {
            "as_of": "2026-11-05",
            "tw": {
                "index": {"close": 42948.91},
                "stocks": {"2330": _stock("台積電"), "2317": _stock("鴻海")},
                "smallcap": {
                    "band_twd": [1e7, 5e7],
                    "stocks": {"6214": {**_stock("精誠"), "rank_score": 0.81}},
                },
                "sectors": {
                    "半導體業": {
                        "n": 80, "avg_return_5d_pct": 1.0, "avg_return_20d_pct": 2.0,
                        "foreign_net_buy_5d_lots": 100, "extra": "x",
                        "leaders": [{"symbol": "2330", "name": "台積電", "return_20d_pct": 9.0}],
                    },
                },
            },
            "news": [{"title": "t"}],
        },
    }


def test_compact_drops_detail_fields(report):
    out = prompts.compact_features_for_chat(report["features"])
    st = out["tw"]["stocks"]
    assert st["columns"] == ["symbol", *prompts.CHAT_STOCK_FIELDS]
    assert [r[0] for r in st["rows"]] == ["2317", "2330"]          # 依代號排序
    row = dict(zip(st["columns"], st["rows"][1]))
    assert row["name"] == "台積電" and row["foreign_net_streak"] == 3
    sc = out["tw"]["smallcap"]["stocks"]
    assert sc["columns"][-1] == "rank_score" and sc["rows"][0][-1] == 0.81
    assert out["tw"]["smallcap"]["band_twd"] == [1e7, 5e7]
    sec = out["tw"]["sectors"]
    assert "extra" not in sec["columns"]
    assert dict(zip(sec["columns"], sec["rows"][0]))["leaders"] == "2330台積電"
    dumped = json.dumps(out, ensure_ascii=False)
    assert "fundamentals" not in dumped and "margin_balance_lots" not in dumped
    assert out["tw"]["index"] == {"close": 42948.91}
    assert out["news"] == [{"title": "t"}]


def test_compact_does_not_mutate_input(report):
    before = json.dumps(report, sort_keys=True)
    prompts.compact_features_for_chat(report["features"])
    assert json.dumps(report, sort_keys=True) == before


def test_chat_system_is_byte_stable_and_compact(report):
    a = prompts.build_chat_system(report)
    b = prompts.build_chat_system(json.loads(json.dumps(report)))
    assert a == b, "前綴不穩定會讓 prompt cache 每題都 miss"
    assert "eps_ttm" not in a and "margin_balance_lots" not in a
    assert "eps_ttm" not in prompts.build_qa_static_block(report)


def test_named_symbol_in_report_gets_full_block(report, monkeypatch):
    def _no_fetch(sym):
        raise AssertionError(f"晨報內已有 {sym}，不該即時查")
    monkeypatch.setattr(ask.tw_features, "build_adhoc_symbol", _no_fetch)
    known = set(report["features"]["tw"]["stocks"])
    out = ask._ondemand_symbols("2330 跟 6214 能買嗎", known,
                                report_blocks=ask._report_stock_blocks(report))
    assert out["2330"]["fundamentals"]["eps_ttm"] == 9.9
    assert out["6214"]["margin_balance_lots"] == 999
    block = prompts.build_qa_variable_block("2330 能買嗎", on_demand=out)
    assert "margin_balance_lots" in block


def test_unnamed_question_has_no_stock_block(report):
    known = set(report["features"]["tw"]["stocks"])
    out = ask._ondemand_symbols("今天大盤怎麼看", known,
                                report_blocks=ask._report_stock_blocks(report))
    assert out == {}


def test_symbol_outside_report_still_fetched(report, monkeypatch):
    monkeypatch.setattr(ask.tw_features, "build_adhoc_symbol", lambda s: {"close": 1.0})
    out = ask._ondemand_symbols("0050 呢", set(), report_blocks=ask._report_stock_blocks(report))
    assert out == {"0050": {"close": 1.0}}


class _FakeHashRedis:
    def __init__(self):
        self.h: dict[str, dict[str, float]] = {}

    def hincrby(self, key, field, n):
        self.h.setdefault(key, {})[field] = self.h.get(key, {}).get(field, 0) + int(n)

    def hincrbyfloat(self, key, field, n):
        self.h.setdefault(key, {})[field] = self.h.get(key, {}).get(field, 0.0) + float(n)

    def hgetall(self, key):
        return {k: str(v) for k, v in self.h.get(key, {}).items()}

    def expire(self, key, ttl):  # noqa: ARG002
        return True


def test_record_ask_accumulates(monkeypatch):
    r = _FakeHashRedis()
    monkeypatch.setattr(ct, "redis_client", r)
    ct.record_ask(6.0, {"input_tokens": 100, "cached_tokens": 2000, "cache_write_tokens": 25000,
                        "output_tokens": 800, "web_search_requests": 1})
    ct.record_ask(4.0, {"input_tokens": 50, "output_tokens": 200})
    stats = ct.ask_stats()
    assert stats["n"] == 2 and stats["twd"] == pytest.approx(10.0)
    assert stats["avg_twd"] == pytest.approx(5.0)
    assert stats["input"] == 150 and stats["cache_write"] == 25000 and stats["searches"] == 1
    assert "fetches" not in stats


def test_record_ask_never_raises(monkeypatch):
    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("redis down")
    monkeypatch.setattr(ct, "redis_client", _Broken())
    ct.record_ask(1.0, {"input_tokens": 1})   # 不拋
    assert ct.ask_stats() == {}
