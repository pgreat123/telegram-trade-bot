"""
Pre-trade safety checks for Robinhood Chain tokens. Every call must pass ALL
of these before the risk manager or execution layer ever sees it.

Uses:
  - DexScreener API for liquidity + basic token info (no key required)
  - A live eth_call sell-simulation against Uniswap V3 QuoterV2 to catch
    honeypots directly on-chain (no third-party API required — 0x and
    GoPlus do not currently support Robinhood Chain, so this is the
    ground-truth check, not a fallback)
  - GoPlus Security API for holder concentration + tax, used as a
    best-effort secondary signal. If GoPlus has no data for this chain,
    that's logged as a warning, NOT a fail — it must not silently block
    every trade the way an unsupported-chain sell-check used to.

None of these are perfect (scammers adapt), but skipping them entirely
against a small bankroll of unknown CA calls is how that bankroll goes to
zero on trade one.
"""
import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
from web3 import Web3
from web3.exceptions import ContractLogicError

from config import settings

log = logging.getLogger("safety")

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"

# --- Robinhood Chain (4663) canonical Uniswap V3 addresses ---
# Confirmed against Uniswap/contracts on GitHub:
# https://github.com/Uniswap/contracts/blob/main/deployments/4663.md
QUOTER_V2_ADDRESS = Web3.to_checksum_address("0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7")
V3_FACTORY_ADDRESS = Web3.to_checksum_address("0x1f7d7550B1b028f7571E69A784071F0205FD2EfA")
FEE_TIERS = [3000, 10000, 500, 100]  # most-to-least common, checked in order

QUOTER_V2_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint24", "name": "fee", "type": "uint24"},
                    {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
                "internalType": "struct IQuoterV2.QuoteExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "quoteExactInputSingle",
        "outputs": [
            {"internalType": "uint256", "name": "amountOut", "type": "uint256"},
            {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"},
            {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"},
            {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"},
        ],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

_w3 = None
_weth_address = None


def _get_w3() -> Web3:
    global _w3
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(settings.chain.rpc_url))
    return _w3


def _get_weth_address() -> str:
    """
    Resolves WETH9 from settings.chain.weth_address rather than a hardcoded
    constant here. That field's docstring in config.py is explicit: verify
    the address against docs.robinhood.com/chain/protocol-contracts yourself
    before setting WETH_ADDRESS — don't trust one pulled from chat/search.
    Raises loudly if unset, since silently falling back to a guessed address
    would defeat the point of that guardrail.
    """
    global _weth_address
    if _weth_address is None:
        raw = settings.chain.weth_address
        if not raw:
            raise RuntimeError(
                "settings.chain.weth_address is not set. Verify the WETH address for "
                "Robinhood Chain against docs.robinhood.com/chain/protocol-contracts "
                "yourself, then set WETH_ADDRESS in your .env. Do not paste an address "
                "from chat/search without verifying it first."
            )
        _weth_address = Web3.to_checksum_address(raw)
    return _weth_address


def _quote_sync(w3, quoter, token_in, token_out, amount_in, fee):
    """Single eth_call quote. Returns amountOut, or None if it reverts."""
    try:
        result = quoter.functions.quoteExactInputSingle(
            (
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                amount_in,
                fee,
                0,  # sqrtPriceLimitX96 = 0 (no limit)
            )
        ).call()
        return result[0]  # amountOut
    except ContractLogicError:
        return None
    except Exception:
        return None


def _simulate_round_trip_sync(token_address: str, probe_wei: int = 10**16) -> dict:
    """
    Simulate buying `probe_wei` worth of WETH into the token, then selling
    the resulting tokens straight back — all via eth_call, no gas spent.
    Runs synchronously; call via asyncio.to_thread from async code.
    """
    w3 = _get_w3()
    quoter = w3.eth.contract(address=QUOTER_V2_ADDRESS, abi=QUOTER_V2_ABI)
    token = Web3.to_checksum_address(token_address)
    weth = _get_weth_address()

    buy_out, working_fee = None, None
    for fee in FEE_TIERS:
        out = _quote_sync(w3, quoter, weth, token, probe_wei, fee)
        if out is not None and out > 0:
            buy_out, working_fee = out, fee
            break

    if buy_out is None:
        return {
            "is_honeypot": True,
            "error": "no live pool found on any standard fee tier, or buy quote reverted",
        }

    sell_out = _quote_sync(w3, quoter, token, weth, buy_out, working_fee)

    if sell_out is None or sell_out == 0:
        return {
            "is_honeypot": True,
            "error": "sell quote reverted or returned zero — cannot sell despite a valid buy quote",
        }

    round_trip_loss_pct = (1 - (sell_out / probe_wei)) * 100
    if round_trip_loss_pct > 50:
        return {
            "is_honeypot": True,
            "error": f"round-trip loss {round_trip_loss_pct:.1f}% exceeds 50% threshold (fee tier {working_fee})",
        }

    return {"is_honeypot": False, "error": ""}


@dataclass
class SafetyResult:
    passed: bool
    reason: str
    liquidity_usd: float = 0.0
    top_holder_pct: float = 0.0


async def check_liquidity(token_address: str, client: httpx.AsyncClient) -> tuple[bool, float, int, str]:
    """Returns (passed, liquidity_usd, pair_created_at_ms, reason)."""
    try:
        resp = await client.get(DEXSCREENER_URL.format(address=token_address), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return False, 0.0, 0, "no trading pair found on DexScreener (too new or not tracked)"

        base_pairs = [p for p in pairs if p.get("chainId") == "robinhood"] or pairs
        best = max(base_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        liquidity_usd = float(best.get("liquidity", {}).get("usd", 0) or 0)
        pair_created_at = int(best.get("pairCreatedAt", 0) or 0)

        if liquidity_usd < settings.safety.min_liquidity_usd:
            return False, liquidity_usd, pair_created_at, (
                f"liquidity too low (${liquidity_usd:,.0f} < "
                f"${settings.safety.min_liquidity_usd:,.0f})"
            )
        return True, liquidity_usd, pair_created_at, ""
    except (httpx.HTTPError, KeyError, ValueError) as e:
        return False, 0.0, 0, f"liquidity check failed: {e}"


async def check_sellable(token_address: str) -> tuple[bool, str]:
    """
    Honeypot check via live eth_call sell-simulation against Uniswap V3
    QuoterV2 on Robinhood Chain. Replaces the old 0x-quote-based check,
    which does not support this chain.
    """
    if not settings.safety.require_sell_check:
        return True, ""

    result = await asyncio.to_thread(_simulate_round_trip_sync, token_address)
    if result["is_honeypot"]:
        return False, result["error"]
    return True, ""


async def check_holders_and_tax(token_address: str, client: httpx.AsyncClient) -> tuple[bool, float, str]:
    """
    GoPlus Security API: checks top-holder concentration and buy/sell tax
    in a single call. Best-effort secondary signal — if GoPlus has no data
    for this chain/token, that's a warning, not a fail (the eth_call sell
    check above is the ground-truth honeypot signal on this chain).
    Returns (passed, top_holder_pct, reason).
    """
    try:
        url = GOPLUS_URL.format(chain_id=settings.chain.chain_id)
        resp = await client.get(url, params={"contract_addresses": token_address}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        result = (data.get("result") or {}).get(token_address.lower())
        if not result:
            log.warning(
                "GoPlus returned no data for %s (chain_id=%s) — likely unsupported chain, "
                "treating as non-blocking",
                token_address, settings.chain.chain_id,
            )
            return True, 0.0, ""

        holders = result.get("holders") or []
        top_pct = 0.0
        for h in holders[:5]:
            if h.get("is_locked") == "1" or h.get("tag") in ("LP", "Burn"):
                continue
            top_pct += float(h.get("percent", 0) or 0)

        if top_pct > settings.safety.max_holder_concentration_pct:
            return False, top_pct, (
                f"top holders control {top_pct:.0%} of supply "
                f"(max {settings.safety.max_holder_concentration_pct:.0%})"
            )

        buy_tax = float(result.get("buy_tax", 0) or 0) * 100
        sell_tax = float(result.get("sell_tax", 0) or 0) * 100
        total_tax = buy_tax + sell_tax
        if total_tax > settings.safety.max_tax_pct:
            return False, top_pct, (
                f"combined tax too high (buy={buy_tax:.1f}% + sell={sell_tax:.1f}% "
                f"> {settings.safety.max_tax_pct:.1f}%)"
            )

        if result.get("is_honeypot") == "1":
            return False, top_pct, "GoPlus flags this as a honeypot"

        return True, top_pct, ""
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("GoPlus check errored for %s, treating as non-blocking: %s", token_address, e)
        return True, 0.0, ""


def check_token_age(pair_created_at_ms: int) -> tuple[bool, str]:
    if settings.safety.min_token_age_minutes <= 0:
        return True, ""
    if pair_created_at_ms <= 0:
        return False, "could not determine pair creation time"
    age_minutes = (time.time() * 1000 - pair_created_at_ms) / 60_000
    if age_minutes < settings.safety.min_token_age_minutes:
        return False, (
            f"token too new ({age_minutes:.1f}m old, "
            f"minimum {settings.safety.min_token_age_minutes}m)"
        )
    return True, ""


async def run_safety_checks(token_address: str) -> SafetyResult:
    async with httpx.AsyncClient() as client:
        liq_ok, liquidity_usd, pair_created_at, liq_reason = await check_liquidity(token_address, client)
        if not liq_ok:
            return SafetyResult(passed=False, reason=liq_reason, liquidity_usd=liquidity_usd)

        age_ok, age_reason = check_token_age(pair_created_at)
        if not age_ok:
            return SafetyResult(passed=False, reason=age_reason, liquidity_usd=liquidity_usd)

        sell_ok, sell_reason = await check_sellable(token_address)
        if not sell_ok:
            return SafetyResult(passed=False, reason=sell_reason, liquidity_usd=liquidity_usd)

        holders_ok, top_holder_pct, holders_reason = await check_holders_and_tax(token_address, client)
        if not holders_ok:
            return SafetyResult(
                passed=False, reason=holders_reason,
                liquidity_usd=liquidity_usd, top_holder_pct=top_holder_pct,
            )

        return SafetyResult(
            passed=True, reason="all checks passed",
            liquidity_usd=liquidity_usd, top_holder_pct=top_holder_pct,
        )