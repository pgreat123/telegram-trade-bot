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

# Some scanner bots post a message first and only attach its trade
# buttons/links a moment later via an edit (once pool data finishes
# loading). Telethon's NewMessage event fires before that edit lands, so we
# also listen for MessageEdited and re-run the same parsing logic on it.
# This set stops the same message from ever triggering on_signal twice
# (e.g. new -> edited-with-buttons -> edited-again-with-updated-stats).
_actioned_message_ids: set[int] = set()


def _extract_urls(message) -> list[str]:
    """
    Pulls every URL referenced by a message, from two places Telethon
    exposes them separately:
      - message.entities: hyperlinks applied to plain text (invisible in
        raw_text) via MessageEntityTextUrl
      - message.buttons: inline keyboard buttons (e.g. "View on GeckoTerminal",
        "Trade on based_eth_bot") — accessed via each button's `.url`

    Some "EARLY CALL" style messages put the contract address only in one
    of these (e.g. a based_eth_bot deep link), never in the visible text —
    and sometimes only after the message is edited in, not on first send.
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


async def _handle_message(event, channel_name: str, on_signal, label: str = "new message"):
    raw_text = event.raw_text or ""
    log.info(f"[{channel_name}] {label}: {raw_text[:120]!r}")

    button_urls = _extract_urls(event.message)
    if button_urls:
        log.info(f"  -> found {len(button_urls)} linked url(s) in message")

    signal = parse_message(raw_text, source_channel=channel_name, button_urls=button_urls)
    if signal is None:
        log.info("  -> no actionable signal parsed from this message")
        return

    message_id = event.message.id
    if signal.token_address and message_id in _actioned_message_ids:
        log.info("  -> signal already actioned for this message id, skipping duplicate")
        return

    if signal.token_address:
        _actioned_message_ids.add(message_id)

    log.info(f"  -> parsed signal: {signal}")
    await on_signal(signal)


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
    async def new_message_handler(event):
        channel_name = getattr(event.chat, "username", None) or str(event.chat_id)
        await _handle_message(event, channel_name, on_signal, label="new message")

    @client.on(events.MessageEdited(chats=settings.telegram.channels))
    async def edited_message_handler(event):
        channel_name = getattr(event.chat, "username", None) or str(event.chat_id)
        await _handle_message(event, channel_name, on_signal, label="message edited")

    await client.start()
    log.info(f"Listening on channels: {settings.telegram.channels}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    async def _print_only(signal):
        print("SIGNAL:", signal)

    asyncio.run(run_listener(_print_only))