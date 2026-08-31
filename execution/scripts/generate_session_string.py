"""
Run this ONCE, locally, on your own machine (not on Railway) to log in
interactively and produce a session string you can paste into
TELEGRAM_SESSION_STRING as a Railway environment variable.

    python -m scripts.generate_session_string

It'll prompt for your phone number and the login code Telegram sends you,
same as the normal first-run flow, then print a long string. Copy that
whole string into Railway's env var TELEGRAM_SESSION_STRING — do NOT put it
in your .env file if that file might ever get committed, and never paste it
anywhere public. Anyone with this string can log in as your Telegram
account, same as your phone number + password would let them.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

from config import settings


async def main():
    if not settings.telegram.api_id or not settings.telegram.api_hash:
        print("Set TELEGRAM_API_ID / TELEGRAM_API_HASH in your .env first.")
        return

    async with TelegramClient(StringSession(), settings.telegram.api_id,
                               settings.telegram.api_hash) as client:
        session_str = client.session.save()
        print("\n" + "=" * 60)
        print("Copy everything between the lines into TELEGRAM_SESSION_STRING")
        print("=" * 60)
        print(session_str)
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())