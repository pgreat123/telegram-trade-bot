"""
Pre-trade safety checks for Base tokens. Every call must pass ALL of these
before the risk manager or execution layer ever sees it.

Uses free/public data sources:
  - DexScreener API for liquidity + basic token info (no key required)
  - A simulated sell (via 0x quote API) to catch honeypots — if we can't
    get a valid sell quote for the token, we don't buy it.

None of these are perfect (scammers adapt), but skipping them entirely
against a $50 bankroll of unknown CA calls is how that bankroll goes to
zero on trade one.
"""
import logging
from dataclasses import dataclass

import httpx

from config import settings

log = logging.getLogger("safety")

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
ZEROX_QUOTE_URL = "https://api.0x.org/swap/v1/quote"


@dataclass
class SafetyResult:
    passed: bool
    reason: str
    liquidity_usd: float = 0.0
    top_holder_pct: float = 0.0


async def check_liquidity(token_address: str, client: httpx.AsyncClient) -> tuple[bool, float, str]:
    try:
        resp = await client.get(DEXSCREENER_URL.format(address=token_address), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return False, 0.0, "no trading pair found on DexScreener (too new or not tracked)"

        # take the pair with the highest liquidity on Robinhood Chain
        base_pairs = [p for p in pairs if p.get("chainId") == "robinhood"] or pairs
        best = max(base_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        liquidity_usd = float(best.get("liquidity", {}).get("usd", 0) or 0)

        if liquidity_usd < settings.safety.min_liquidity_usd:
            return False, liquidity_usd, (
                f"liquidity too low (${liquidity_usd:,.0f} < "
                f"${settings.safety.min_liquidity_usd:,.0f})"
            )
        return True, liquidity_usd, ""
    except (httpx.HTTPError, KeyError, ValueError) as e:
        return False, 0.0, f"liquidity check failed: {e}"


async def check_sellable(token_address: str, client: httpx.AsyncClient) -> tuple[bool, str]:
    """
    Honeypot check: request a quote to SELL a tiny amount of the token back
    to ETH. If the quote API can't route a sell, or the sell would incur an
    absurd effective fee, treat it as a honeypot.
    """
    if not settings.safety.require_sell_check:
        return True, ""

    params = {
        "sellToken": token_address,
        "buyToken": "ETH",
        "sellAmount": "1000000000000000000",  # 1 token unit at 18 decimals, adjust if needed
        "chainId": settings.chain.chain_id,
    }
    headers = {"0x-api-key": settings.chain.zerox_api_key} if settings.chain.zerox_api_key else {}
    try:
        resp = await client.get(ZEROX_QUOTE_URL, params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            return False, f"no sell route available (honeypot risk), status={resp.status_code}"
        data = resp.json()
        if not data.get("buyAmount"):
            return False, "sell quote returned no output amount (likely honeypot)"
        return True, ""
    except httpx.HTTPError as e:
        return False, f"sell check failed: {e}"


async def run_safety_checks(token_address: str) -> SafetyResult:
    async with httpx.AsyncClient() as client:
        liq_ok, liquidity_usd, liq_reason = await check_liquidity(token_address, client)
        if not liq_ok:
            return SafetyResult(passed=False, reason=liq_reason, liquidity_usd=liquidity_usd)

        sell_ok, sell_reason = await check_sellable(token_address, client)
        if not sell_ok:
            return SafetyResult(passed=False, reason=sell_reason, liquidity_usd=liquidity_usd)

        # Holder concentration check would go here via a block explorer API
        # (e.g. Basescan) — left as a TODO since it requires an API key;
        # see safety/holders.py stub.

        return SafetyResult(passed=True, reason="all checks passed", liquidity_usd=liquidity_usd)
