"""持股追蹤提醒（reports/holdings_alert.py）。

釘住三件事：穿越判定只在穿越當天響、訊息只送私人頻道、持股內容不進公開的晨報。
"""
from __future__ import annotations

import json

import pytest

from reports import holdings_alert as ha

BLOCK = {
    "name": "臻鼎-KY", "as_of": "2026-11-05", "close": 394.0,
    "return_1d_pct": -9.01, "return_5d_pct": -24.23, "return_20d_pct": -31.6,
    "vs_index_20d_pct": -20.2, "foreign_net_buy_5d_lots": -15370, "foreign_net_streak": -9,
    "trust_net_streak": 19, "margin_chg_5d_lots": -4410,
}
FUND = {"revenue_month": "2026-09", "revenue_100m": 263.6, "revenue_yoy_pct": 36.1}
H = ha.Holding(symbol="4958", breakeven=445, alert_below=[368, 357], alert_above=[445, 490])


@pytest.fixture
def data(monkeypatch):
    """假資料：今日 394、前日 433。"""
    state = {"block": dict(BLOCK), "prev": 433.0, "fund": dict(FUND)}
    from processor import tw_features
    monkeypatch.setattr(tw_features, "build_adhoc_symbol", lambda s: state["block"])
    monkeypatch.setattr(ha, "_prev_close", lambda s: state["prev"])
    monkeypatch.setattr(ha, "_fundamentals", lambda s: state["fund"])
    return state


# ── 設定檔 ────────────────────────────────────────────────────────────

def test_missing_file_means_no_holdings(tmp_path):
    assert ha.load_holdings(tmp_path / "nope.json") == {}


@pytest.mark.parametrize("content", ["{壞掉", "[1, 2]", '{"hank": [{"breakeven": 1}]}'])
def test_bad_file_is_ignored_not_raised(tmp_path, content):
    p = tmp_path / "holdings.json"
    p.write_text(content, encoding="utf-8")
    assert ha.load_holdings(p) == {}


def test_loads_and_skips_comment_keys(tmp_path):
    p = tmp_path / "holdings.json"
    p.write_text(json.dumps({"_說明": "x", "Hank": [H.model_dump()]}), encoding="utf-8")
    out = ha.load_holdings(p)
    assert list(out) == ["hank"] and out["hank"][0].breakeven == 445


def test_example_config_is_valid():
    from pathlib import Path
    example = Path(__file__).resolve().parent.parent / "backend" / "configs" / "holdings.example.json"
    assert ha.load_holdings(example)["hank"][0].symbol == "2330"


# ── 穿越判定 ──────────────────────────────────────────────────────────

def test_cross_down_fires_on_crossing_day():
    assert ha.crossings(380, 366, [368, 357], []) == ["跌破 368"]


def test_cross_through_two_levels_lists_both():
    assert ha.crossings(380, 350, [368, 357], []) == ["跌破 368", "跌破 357"]


def test_already_below_does_not_fire_again():
    """跌破後每天都響＝提醒失效。"""
    assert ha.crossings(366, 360, [368], []) == []


def test_cross_up_and_exact_touch():
    assert ha.crossings(440, 445, [], [445, 490]) == ["站上 445"]
    assert ha.crossings(370, 368, [368], []) == ["跌破 368"]      # 收在價位上也算跌到


def test_no_prev_close_never_fires():
    assert ha.crossings(None, 300, [368], [445]) == []


# ── 訊息內容 ──────────────────────────────────────────────────────────

def test_message_matches_analysis_numbers(data):
    text = ha.build_messages({"hank": [H]})["hank"]
    assert "4958 臻鼎-KY　394（-9.0%）" in text
    assert "距損平 445：-11.5%" in text
    assert "外資 連賣9日、5日 -15,370張" in text and "投信 連買19日" in text
    assert "下方 368（-6.6%）・357（-9.4%）" in text
    assert "上方 445（+12.9%）・490（+24.4%）" in text
    assert "9月營收 263.6億 YoY +36.1%" in text
    assert "🔔" not in text                     # 433 → 394 沒穿越任何設定價位
    assert "資料日 2026-11-05" in text and ha.FOOTER in text


def test_crossing_shows_bell(data):
    data["block"]["close"] = 366.0
    data["prev"] = 394.0
    assert "🔔 今日收盤跌破 368" in ha.build_messages({"hank": [H]})["hank"]


def test_missing_data_degrades_gracefully(data):
    data["block"] = None
    data["fund"] = None
    text = ha.build_messages({"hank": [H]})["hank"]
    assert "查無行情資料" in text


def test_message_is_capped_for_discord(data):
    many = [ha.Holding(symbol=f"{1000 + i}", breakeven=100, alert_below=[90, 80],
                       alert_above=[110, 120]) for i in range(40)]
    assert len(ha.build_messages({"hank": many})["hank"]) <= ha.MAX_MESSAGE_CHARS


# ── 送出：只送私人頻道 ────────────────────────────────────────────────

def test_sends_to_users_private_channel_only(data, monkeypatch):
    from notify import discord
    sent = []
    monkeypatch.setattr(discord, "send_message", lambda ch, text: sent.append(ch) or True)
    monkeypatch.setattr(ha.settings, "discord_hank_chat_channel_id", "HANK_PRIVATE")
    monkeypatch.setattr(ha.settings, "discord_daily_report_channel_id", "DAILY_SHARED")
    assert ha.send_holdings_alerts({"hank": [H]}) == {"hank": True}
    assert sent == ["HANK_PRIVATE"]


def test_user_without_channel_is_skipped_not_rerouted(data, monkeypatch):
    from notify import discord
    sent = []
    monkeypatch.setattr(discord, "send_message", lambda ch, text: sent.append(ch) or True)
    monkeypatch.setattr(ha.settings, "discord_hank_chat_channel_id", "")
    assert ha.send_holdings_alerts({"hank": [H]}) == {"hank": False}
    assert sent == []


def test_dry_run_cli_prints_without_sending(data, monkeypatch, tmp_path, capsys):
    from notify import discord
    p = tmp_path / "holdings.json"
    p.write_text(json.dumps({"hank": [H.model_dump()]}), encoding="utf-8")
    monkeypatch.setattr(ha.settings, "local_storage_path", str(tmp_path))
    monkeypatch.setattr(discord, "send_message", lambda *a: pytest.fail("dry-run 不得送出"))
    assert ha.main(["--dry-run"]) == 0
    assert "距損平 445" in capsys.readouterr().out
