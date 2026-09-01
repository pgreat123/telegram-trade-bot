"""
Telegram listener — connects as your own user account (via Telethon) and
watches the configured channels for new messages, handing each one to the
signal parser.

Uses a userbot session (not a bot-account token) because bot accounts can't
read channel history / join arbitrary channels the way a normal account can.
First run will prompt you to log in interactively (phone number + code);
after that it reuses a saved session file.
"""
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl
import asyncio
import logging

from config import settings
from parser.signal_parser import parse_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("listener")


def _extract_urls(message) -> list[str]:
    """
    Pulls every URL referenced by a message, from two places Telethon
    exposes them separately:
      - message.entities: hyperlinks applied to plain text (invisible in
        raw_text) via MessageEntityTextUrl
      - message.buttons: inline keyboard buttons (e.g. "View on GeckoTerminal",
        "Trade on based_eth_bot") — accessed via each button's `.url`

    Some "EARLY CALL" style messages put the contract address only in one
    of these (e.g. a based_eth_bot deep link), never in the visible text.
    """
    urls: list[str] = []

    entities = message.entities or []
    for entity in entities:
        if isinstance(entity, MessageEntityTextUrl) and entity.url:
            urls.append(entity.url)

    for row in (message.buttons or []):
        for button in row:
            url = getattr(button, "url", None)
            if url:
                urls.append(url)

    return urls


async def run_listener(on_signal):
    """
    on_signal: async callback(signal: ParsedSignal) invoked for every
    successfully parsed trading signal.
    """
    if not settings.telegram.api_id or not settings.telegram.api_hash:
        raise RuntimeError(
            "Missing TELEGRAM_API_ID / TELEGRAM_API_HASH — set them in your .env file. "
            "Get them from https://my.telegram.org"
        )
    if not settings.telegram.channels:
        raise RuntimeError(
            "No channels configured — set TELEGRAM_CHANNELS in your .env file "
            "(comma-separated usernames or invite links)."
        )

    session = (
        StringSession(settings.telegram.session_string)
        if settings.telegram.session_string
        else settings.telegram.session_name
    )
    client = TelegramClient(
        session,
        settings.telegram.api_id,
        settings.telegram.api_hash,
    )

    @client.on(events.NewMessage(chats=settings.telegram.channels))
    async def handler(event):
        raw_text = event.raw_text or ""
        channel_name = getattr(event.chat, "username", None) or str(event.chat_id)
        log.info(f"[{channel_name}] new message: {raw_text[:120]!r}")

        button_urls = _extract_urls(event.message)
        if button_urls:
            log.info(f"  -> found {len(button_urls)} linked url(s) in message")

        signal = parse_message(raw_text, source_channel=channel_name, button_urls=button_urls)
        if signal is None:
            log.info("  -> no actionable signal parsed from this message")
            return

        log.info(f"  -> parsed signal: {signal}")
        await on_signal(signal)

    await client.start()
    log.info(f"Listening on channels: {settings.telegram.channels}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    async def _print_only(signal):
        print("SIGNAL:", signal)

    asyncio.run(run_listener(_print_only))