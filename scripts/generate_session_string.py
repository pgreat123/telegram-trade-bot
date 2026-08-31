"""
Run this ONCE, locally, on your own machine (not on Railway) to log in
interactively and produce a session string, which gets written directly
into your .env file as TELEGRAM_SESSION_STRING.

    python -m scripts.generate_session_string

It will prompt for your phone number and the login code Telegram sends you,
same as the normal first-run flow, then write the string straight into
.env -- do NOT let .env get committed to git, and never paste this string
anywhere public. Anyone with it can log in as your Telegram account, same
as your phone number + password would let them.
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

        env_path = ".env"
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        found = False
        for i, line in enumerate(lines):
            if line.startswith("TELEGRAM_SESSION_STRING="):
                lines[i] = f"TELEGRAM_SESSION_STRING={session_str}\n"
                found = True
                break
        if not found:
            lines.append(f"TELEGRAM_SESSION_STRING={session_str}\n")

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        print("\nDone -- TELEGRAM_SESSION_STRING has been written directly to .env")


if __name__ == "__main__":
    asyncio.run(main())
