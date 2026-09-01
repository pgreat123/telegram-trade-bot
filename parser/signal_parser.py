"""
Parses raw Telegram messages from the two channel types into a structured
ParsedSignal:

  - "CA + thesis" channel: message contains a contract address and some
    surrounding text explaining why the caller likes it.
  - "Live buys / Xs" channel: message announces a buy, and later messages
    update on how many multiples ("Xs") that call has done.

This is intentionally permissive on parsing (regex-based) but conservative
on what counts as "actionable" — a bare CA with no other context is treated
differently from a fresh, unambiguous buy call. Tune the patterns below as
you see real message formats from your two channels.

Some "EARLY CALL" style messages carry the contract address only inside a
linked bot/gecko URL (e.g. a Telegram inline button or a hyperlinked word),
not in the plain message text at all. `parse_message` accepts an optional
list of `button_urls` for exactly this case, and falls back to scanning
those URLs for an address when the plain text has none.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# EVM contract address: 0x followed by 40 hex chars (works for Robinhood
# Chain since it's fully EVM-compatible — same address format as Ethereum)
EVM_CA_PATTERN = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

# Looser variant for scanning inside URLs: deep-link params commonly prefix
# the address with an underscore (e.g. "...?start=r_scout_b_0x1234...").
# "_" counts as a \w character, so a leading \b boundary would never match
# right after it — this pattern drops that leading boundary requirement.
CA_IN_URL_PATTERN = re.compile(r"0x[a-fA-F0-9]{40}\b")

# Loose "X multiple" pattern, e.g. "3x", "12.5X", "did 4x"
X_MULTIPLE_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*[xX]\b")

BUY_KEYWORDS = re.compile(r"\b(buy|bought|entry|ape|aping|long)\b", re.IGNORECASE)
SELL_KEYWORDS = re.compile(r"\b(sell|sold|exit|dump|dumped)\b", re.IGNORECASE)


@dataclass
class ParsedSignal:
    token_address: str
    action: str              # "buy" or "sell" or "update"
    source_channel: str
    raw_text: str
    received_at: str
    x_multiple: Optional[float] = None   # populated for "Xs done" update messages
    has_thesis: bool = False


def _find_ca_in_urls(urls: list[str]) -> Optional[str]:
    """Scan a list of URLs (from buttons/entities) for an embedded EVM address."""
    for url in urls:
        match = CA_IN_URL_PATTERN.search(url)
        if match:
            return match.group(0)
    return None


def parse_message(
    text: str,
    source_channel: str,
    button_urls: Optional[list[str]] = None,
) -> Optional[ParsedSignal]:
    text = text.strip()
    button_urls = button_urls or []
    if not text:
        return None

    ca_match = EVM_CA_PATTERN.search(text)
    ca_from_text = ca_match.group(0) if ca_match else None
    ca_address = ca_from_text or _find_ca_in_urls(button_urls)

    x_match = X_MULTIPLE_PATTERN.search(text)

    is_buy = bool(BUY_KEYWORDS.search(text))
    is_sell = bool(SELL_KEYWORDS.search(text))

    # Case 1: message has a contract address (in text OR in a linked
    # bot/gecko URL) -> real actionable call
    if ca_address:
        action = "sell" if is_sell else "buy"  # default to buy unless explicit sell
        # crude thesis heuristic: message has meaningful text beyond just the address
        remaining_text = text.replace(ca_from_text, "").strip() if ca_from_text else text
        has_thesis = len(remaining_text) > 25
        return ParsedSignal(
            token_address=ca_address,
            action=action,
            source_channel=source_channel,
            raw_text=text,
            received_at=datetime.utcnow().isoformat(),
            has_thesis=has_thesis,
        )

    # Case 2: no CA anywhere, but this looks like an "Xs done" performance
    # update. These don't carry a fresh address themselves (they refer back
    # to an earlier call) — return them as an "update" signal so the caller
    # can log performance, but they are NOT independently actionable without
    # matching them to a previously seen token. That matching is left to
    # the caller/state layer, not this parser.
    if x_match:
        return ParsedSignal(
            token_address="",
            action="update",
            source_channel=source_channel,
            raw_text=text,
            received_at=datetime.utcnow().isoformat(),
            x_multiple=float(x_match.group(1)),
        )

    return None