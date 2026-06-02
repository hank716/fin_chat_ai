"""Discord 互動 bot（M4，對齊 design_docs §21/§9.2）。

設計：1 guild / 3 channels / 2 users。
- daily-report 頻道：唯讀廣播（每日晨報由 backend 直接推），bot 不在此回應。
- jay-chat / hank-chat：各自專屬頻道；bot **只回應該頻道對應的 user**（別人發言忽略）。
- 使用者問題 → backend POST /ask（grounded 在最新晨報 + 成本上限）→ 回覆答案 + 💰 成本。

bot 是薄轉接層：不直接呼叫 Gemini，不碰資料；Gemini/guardrail/成本都在 backend。
需在 Discord Developer Portal 開啟 MESSAGE CONTENT INTENT。
"""
from __future__ import annotations

import logging
import os

import discord
import httpx

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("ai-market-bot")

TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")
DAILY_CHANNEL = os.environ.get("DISCORD_DAILY_REPORT_CHANNEL_ID", "").strip()

# channel_id -> 該頻道唯一允許互動的 user_id
ALLOWED: dict[str, str] = {}
for ch_env, user_env in (
    ("DISCORD_JAY_CHAT_CHANNEL_ID", "DISCORD_JAY_USER_ID"),
    ("DISCORD_HANK_CHAT_CHANNEL_ID", "DISCORD_HANK_USER_ID"),
):
    ch = os.environ.get(ch_env, "").strip()
    user = os.environ.get(user_env, "").strip()
    if ch and user:
        ALLOWED[ch] = user

ASK_TIMEOUT = float(os.environ.get("ASK_TIMEOUT", "120"))

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def _ask_backend(user_id: str, question: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=ASK_TIMEOUT) as http:
            resp = await http.post(
                f"{BACKEND_URL}/ask", json={"user_id": user_id, "question": question}
            )
        if resp.status_code != 200:
            logger.error("/ask HTTP %s: %s", resp.status_code, resp.text[:200])
            return "⚠️ 查詢暫時無法完成，請稍後再試。"
        d = resp.json()
        answer = d.get("answer", "（無回應）")
        if d.get("budget_exceeded"):
            return answer
        cost_line = (
            f"\n\n💰 本次估算：NT${d.get('cost_twd', 0):.2f}"
            f"　📊 今日累計：NT${d.get('today_spent', 0):.1f} / NT${d.get('daily_limit', 0):.0f}"
        )
        return (answer + cost_line)[:1990]
    except Exception as exc:  # noqa: BLE001
        logger.error("/ask 例外: %s", exc)
        return "⚠️ 查詢發生錯誤，請稍後再試。"


@client.event
async def on_ready() -> None:
    logger.info("bot 已連線：%s；管理頻道=%s；daily=%s",
                client.user, list(ALLOWED.keys()), DAILY_CHANNEL)


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    cid, uid = str(message.channel.id), str(message.author.id)

    if cid == DAILY_CHANNEL:
        return  # 廣播頻道，不互動

    allowed_user = ALLOWED.get(cid)
    if allowed_user is None or uid != allowed_user:
        return  # 非管理頻道，或不是該頻道的擁有者 → 忽略

    question = (message.content or "").strip()
    if not question:
        return

    async with message.channel.typing():
        reply = await _ask_backend(uid, question)
    await message.reply(reply, mention_author=False)


def main() -> None:
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN 未設定")
    if not ALLOWED:
        logger.warning("未設定任何互動頻道（jay/hank channel+user），bot 只會待機")
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
