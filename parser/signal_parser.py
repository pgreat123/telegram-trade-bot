# Matches "$TOKEN hit 3X" / "hit 12.5X" style performance-update posts.
# Checked BEFORE the CA branch below: these messages sometimes carry
# stale buttons/links left over from the original call (the scanner bot
# appears to edit the same message object as it updates stats), so a CA
# can show up in button_urls even though this is an update, not a fresh
# call. Text shape decides the message type; CA presence alone does not.
UPDATE_MESSAGE_PATTERN = re.compile(r"\bhit\s+\d+(?:\.\d+)?\s*[xX]\b", re.IGNORECASE)


def parse_message(
    text: str,
    source_channel: str,
    button_urls: Optional[list[str]] = None,
) -> Optional[ParsedSignal]:
    text = text.strip()
    button_urls = button_urls or []
    if not text:
        return None

    x_match = X_MULTIPLE_PATTERN.search(text)

    # Case 1 (checked first): "hit Xx" performance update. Never
    # actionable as a buy, even if a CA is present in the text or in
    # carried-over button URLs -- see UPDATE_MESSAGE_PATTERN docstring.
    if UPDATE_MESSAGE_PATTERN.search(text):
        return ParsedSignal(
            token_address="",
            action="update",
            source_channel=source_channel,
            raw_text=text,
            received_at=datetime.utcnow().isoformat(),
            x_multiple=float(x_match.group(1)) if x_match else None,
        )

    ca_match = EVM_CA_PATTERN.search(text)
    ca_from_text = ca_match.group(0) if ca_match else None
    ca_address = ca_from_text or _find_ca_in_urls(button_urls)

    is_buy = bool(BUY_KEYWORDS.search(text))
    is_sell = bool(SELL_KEYWORDS.search(text))

    # Case 2: message has a contract address (in text OR in a linked
    # bot/gecko URL) -> real actionable call
    if ca_address:
        action = "sell" if is_sell else "buy"
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

    # Case 3: no CA, no "hit Xx" pattern either -> nothing actionable
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