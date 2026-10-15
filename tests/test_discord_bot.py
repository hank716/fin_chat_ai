"""spec 011 測試基線：頻道/使用者權限、daily-report 不互動、討論串記憶、/ask 失敗友善訊息、
逾限不附成本、長度截斷、REST 推播失敗不拋。

bot 是獨立服務（自己的 requirements），測試環境不裝 discord.py——以空殼模組頂替後按路徑載入。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fake_discord() -> types.ModuleType:
    d = types.ModuleType("discord")

    class Intents:
        message_content = False

        @classmethod
        def default(cls):
            return cls()

    class Client:
        def __init__(self, **k):
            self.user = "bot"

        def event(self, fn):
            return fn

    class Thread:
        pass

    d.Intents, d.Client, d.Thread, d.Message = Intents, Client, Thread, object
    return d


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())
    for k, v in {"DISCORD_DAILY_REPORT_CHANNEL_ID": "100", "DISCORD_JAY_CHAT_CHANNEL_ID": "200",
                 "DISCORD_JAY_USER_ID": "u-jay", "DISCORD_HANK_CHAT_CHANNEL_ID": "300",
                 "DISCORD_HANK_USER_ID": "u-hank"}.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("bot_under_test", ROOT / "bot" / "bot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False


def _message(bot, *, channel_id, user_id, content="2330 怎麼看", thread_parent=None, is_bot=False):
    if thread_parent is not None:
        channel = bot.discord.Thread()
        channel.parent_id = thread_parent
    else:
        channel = types.SimpleNamespace()
    channel.id = channel_id
    channel.typing = lambda: _Typing()
    replies = []

    async def reply(text, mention_author=True):
        replies.append(text)

    msg = types.SimpleNamespace(author=types.SimpleNamespace(id=user_id, bot=is_bot),
                                channel=channel, content=content, reply=reply)
    return msg, replies


@pytest.fixture
def asked(bot, monkeypatch):
    calls = []

    async def fake_ask(uid, question, conversation_id=None):
        calls.append((uid, question, conversation_id))
        return "答"

    monkeypatch.setattr(bot, "_ask_backend", fake_ask)
    return calls


# ── T009 對應使用者在專屬頻道 → 轉 /ask；他人或越權 → 忽略 ──

def test_owner_in_own_channel_is_forwarded(bot, asked):
    msg, replies = _message(bot, channel_id=200, user_id="u-jay")
    asyncio.run(bot.on_message(msg))
    assert asked == [("u-jay", "2330 怎麼看", None)] and replies == ["答"]


@pytest.mark.parametrize("channel_id,user_id,is_bot,content", [
    (200, "u-hank", False, "x"),      # 別人跑進 jay 的頻道
    (300, "u-jay", False, "x"),       # jay 跑進 hank 的頻道
    (999, "u-jay", False, "x"),       # 非管理頻道
    (200, "u-jay", True, "x"),        # bot 自己
    (200, "u-jay", False, "   "),     # 空訊息
])
def test_others_bots_and_empty_messages_are_ignored(bot, asked, channel_id, user_id, is_bot, content):
    msg, replies = _message(bot, channel_id=channel_id, user_id=user_id, is_bot=is_bot, content=content)
    asyncio.run(bot.on_message(msg))
    assert asked == [] and replies == []


# ── T010 daily-report 頻道（含其下討論串）不互動 ──

@pytest.mark.parametrize("kw", [{"channel_id": 100}, {"channel_id": 555, "thread_parent": 100}])
def test_daily_report_channel_and_its_threads_are_silent(bot, asked, kw):
    msg, replies = _message(bot, user_id="u-jay", **kw)
    asyncio.run(bot.on_message(msg))
    assert asked == [] and replies == []


# ── T011 討論串帶 conversation_id（依 parent 判權限）；一般頻道不帶 ──

def test_thread_passes_its_own_id_as_conversation(bot, asked):
    msg, _ = _message(bot, channel_id=777, user_id="u-jay", thread_parent=200)
    asyncio.run(bot.on_message(msg))
    assert asked == [("u-jay", "2330 怎麼看", "777")]


def test_thread_under_someone_elses_channel_is_ignored(bot, asked):
    msg, _ = _message(bot, channel_id=777, user_id="u-jay", thread_parent=300)
    asyncio.run(bot.on_message(msg))
    assert asked == []


# ── T012 _ask_backend：非 200 / 例外 → 友善字串；逾限不附成本行 ──

class _Http:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.sent = resp, exc, []

    def __call__(self, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *e):
        return False

    async def post(self, url, json=None):
        self.sent.append(json)
        if self.exc:
            raise self.exc
        return self.resp


def _resp(status=200, body=None):
    return types.SimpleNamespace(status_code=status, text="err", json=lambda: body or {})


def _ask(bot, monkeypatch, http, conversation_id=None):
    monkeypatch.setattr(bot.httpx, "AsyncClient", http)
    return asyncio.run(bot._ask_backend("u-jay", "q", conversation_id))


def test_backend_http_error_gives_friendly_text(bot, monkeypatch):
    assert _ask(bot, monkeypatch, _Http(_resp(503))).startswith("⚠️ 查詢暫時無法完成")


def test_backend_exception_gives_friendly_text(bot, monkeypatch):
    assert _ask(bot, monkeypatch, _Http(exc=ConnectionError("down"))).startswith("⚠️ 查詢發生錯誤")


def test_budget_exceeded_reply_has_no_cost_line(bot, monkeypatch):
    out = _ask(bot, monkeypatch, _Http(_resp(body={"answer": "今日額度已用完", "budget_exceeded": True})))
    assert out == "今日額度已用完"


def test_normal_reply_appends_cost_line_and_passes_conversation(bot, monkeypatch):
    http = _Http(_resp(body={"answer": "答", "cost_twd": 1.234, "today_spent": 10.0, "daily_limit": 45}))
    out = _ask(bot, monkeypatch, http, conversation_id="777")
    assert out.startswith("答") and "NT$1.23" in out and "NT$10.0 / NT$45" in out
    assert http.sent == [{"user_id": "u-jay", "question": "q", "conversation_id": "777"}]


def test_general_channel_does_not_send_conversation_id(bot, monkeypatch):
    http = _Http(_resp(body={"answer": "答"}))
    _ask(bot, monkeypatch, http)
    assert "conversation_id" not in http.sent[0]


# ── T014 回覆截斷到 Discord 長度限制內 ──

def test_long_reply_is_truncated_under_discord_limit(bot, monkeypatch):
    out = _ask(bot, monkeypatch, _Http(_resp(body={"answer": "長" * 5000, "cost_twd": 1})))
    assert len(out) <= 2000


# ── T013 REST 推播：未設 token/channel → False；HTTP 失敗只記 log ──

@pytest.fixture
def notify(monkeypatch):
    from notify import discord as nd
    monkeypatch.setattr(nd.settings, "discord_token", "tok")
    return nd


@pytest.mark.parametrize("token,channel", [("", "1"), ("tok", "")])
def test_send_message_without_config_returns_false(notify, monkeypatch, token, channel):
    monkeypatch.setattr(notify.settings, "discord_token", token)
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: pytest.fail("不該發請求"))
    assert notify.send_message(channel, "hi") is False


def test_send_message_http_failure_and_exception_return_false(notify, monkeypatch):
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: _resp(403))
    assert notify.send_message("1", "hi") is False

    def boom(*a, **k):
        raise ConnectionError("discord down")

    monkeypatch.setattr(notify.httpx, "post", boom)
    assert notify.send_message("1", "hi") is False


def test_send_message_success(notify, monkeypatch):
    sent = []
    monkeypatch.setattr(notify.httpx, "post",
                        lambda url, headers=None, json=None, timeout=None: sent.append((url, headers, json))
                        or _resp(201))
    assert notify.send_message("42", "hi") is True
    url, headers, body = sent[0]
    assert url.endswith("/channels/42/messages") and headers["Authorization"] == "Bot tok"
    assert body == {"content": "hi"}
