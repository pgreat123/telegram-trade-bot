"""
Telegram notifier — sends live DMs about what the bot is doing, using a
proper Telegram BOT account (different from the listener, which logs in
as your own user account to read channels).

Setup:
  1. Message @BotFather on Telegram, send /newbot, follow the prompts.
     You'll get back a token like "123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxx".
  2. Send /start (or any message) to your new bot from the Telegram account
     you want notifications sent to — the bot can't message you first.
  3. Run `python -m notifier.get_chat_id` (see below) to find your chat ID,
     or visit https://api.telegram.org/bot<TOKEN>/getUpdates after step 2
     and read the "chat":{"id": ...} field.
  4. Put both values in .env as TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

If either value is missing, notifications are silently skipped (logged at
debug level) so the bot still runs fine without this feature configured.

Sending never blocks or crashes the trading pipeline: failures are caught
and logged, never raised.
"""
import html
import logging

import httpx

from config import settings

log = logging.getLogger("notifier")

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _enabled() -> bool:
    return bool(settings.notifier.bot_token and settings.notifier.chat_id)


async def notify(text: str):
    """Fire-and-forget a Telegram DM. Never raises — logs and swallows errors
    so a notification failure can never take down the trading loop."""
    if not _enabled():
        log.debug("Notifier not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing) — skipping: %s", text)
        return

    url = TELEGRAM_API_URL.format(token=settings.notifier.bot_token)
    payload = {
        "chat_id": settings.notifier.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                log.warning("Telegram notify failed (%s): %s", resp.status_code, resp.text[:200])
    except httpx.HTTPError as e:
        log.warning("Telegram notify failed: %s", e)


# ---------- pre-formatted event helpers ----------
# Keeping the formatting here (rather than scattered at call sites) means
# every call site just passes data, and message wording stays consistent.
#
# IMPORTANT: parse_mode is HTML, so any dynamic text that isn't a deliberate
# <b>/<code> tag we're adding ourselves MUST be html.escape()'d first. A
# safety-check "reason" string can contain "<" or ">" (e.g. "liquidity too
# low ($2 < $3,000)") which Telegram's HTML parser otherwise reads as a
# broken tag and rejects the whole message with a 400.

def _short(addr: str) -> str:
    return addr if len(addr) <= 12 else f"{addr[:6]}...{addr[-4:]}"


async def notify_buy_executed(token_address: str, amount_usd: float,
                               liquidity_usd: float, dry_run: bool):
    prefix = "🧪 [DRY RUN] Would buy" if dry_run else "✅ BUY executed"
    await notify(
        f"{prefix}\n"
        f"Token: <code>{html.escape(_short(token_address))}</code>\n"
        f"Size: ${amount_usd:.2f}\n"
        f"Liquidity: ${liquidity_usd:,.0f}"
    )


async def notify_blocked(token_address: str, stage: str, reason: str):
    label = "blocked by risk manager" if stage == "risk" else "blocked by safety check"
    await notify(
        f"🚫 Trade {label}\n"
        f"Token: <code>{html.escape(_short(token_address))}</code>\n"
        f"Reason: {html.escape(reason)}"
    )


async def notify_scale_out(symbol: str, fraction: float, reason: str, dry_run: bool):
    prefix = "🧪 [DRY RUN] Would scale out" if dry_run else "📉 Scaled out"
    await notify(
        f"{prefix}\n"
        f"Token: <code>{html.escape(symbol)}</code>\n"
        f"Sold: {fraction:.0%} of original size\n"
        f"Reason: {html.escape(reason)}"
    )


async def notify_position_closed(symbol: str, reason: str, pnl_usd: float | None, dry_run: bool):
    if dry_run:
        await notify(
            f"🧪 [DRY RUN] Would close full position\n"
            f"Token: <code>{html.escape(symbol)}</code>\n"
            f"Reason: {html.escape(reason)}"
        )
        return

    emoji = "🟢" if (pnl_usd or 0) >= 0 else "🔴"
    pnl_line = f"Realized P&L: {emoji} ${pnl_usd:.2f}\n" if pnl_usd is not None else ""
    await notify(
        f"{emoji} Position closed\n"
        f"Token: <code>{html.escape(symbol)}</code>\n"
        f"{pnl_line}"
        f"Reason: {html.escape(reason)}"
    )