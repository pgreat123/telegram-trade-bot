"""
One-off helper: prints the chat ID(s) of anyone who has messaged your
notifier bot. Run this AFTER you've sent at least one message to the bot
(e.g. /start) from the Telegram account you want notifications sent to.

Usage:
    python -m notifier.get_chat_id
"""
import httpx

from config import settings


def main():
    token = settings.notifier.bot_token
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env — set it first (see notifier/telegram_notifier.py docstring).")
        return

    resp = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("result"):
        print("No messages found yet. Send /start (or anything) to your bot on Telegram, then re-run this.")
        return

    seen = set()
    for update in data["result"]:
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat = msg["chat"]
        key = (chat["id"], chat.get("type"))
        if key in seen:
            continue
        seen.add(key)
        name = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
        print(f"chat_id={chat['id']}  type={chat.get('type')}  name={name}")

    print("\nCopy the chat_id for your account into .env as TELEGRAM_CHAT_ID.")


if __name__ == "__main__":
    main()
