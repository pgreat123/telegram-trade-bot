"""
Execution layer — places real swaps on Robinhood Chain using the 0x API
for routing (0x confirmed Day-1 support for Robinhood Chain) and web3.py
for signing/sending the transaction.

Respects settings.dry_run: when True (the default), trades are logged and
simulated but NEVER sent on-chain. Flip DRY_RUN=false in your .env only
once you've watched the bot's logged decisions and trust them.
"""
import logging

import httpx
from web3 import Web3

from config import settings

log = logging.getLogger("executor")

# 0x uses a single unified endpoint across chains, selected via chainId param.
ZEROX_QUOTE_URL = "https://api.0x.org/swap/v1/quote"

# WETH on Robinhood Chain mainnet: verify this yourself against
# https://docs.robinhood.com/chain/protocol-contracts before setting it.
# There is a documented, active scam ecosystem of fake WETH/USDC clones on
# this specific chain — do NOT trust an address from a Telegram post, a
# search result, or this comment. Set the real one in .env as WETH_ADDRESS.
WETH_ROBINHOOD = settings.chain.weth_address


class Executor:
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(settings.chain.rpc_url))
        self.account = None
        if settings.chain.wallet_private_key:
            self.account = self.w3.eth.account.from_key(settings.chain.wallet_private_key)

    async def buy(self, token_address: str, amount_usd: float) -> dict:
        """Buy `token_address` using approximately `amount_usd` worth of ETH."""
        eth_price = await self._get_eth_price_usd()
        amount_eth = amount_usd / eth_price
        amount_wei = self.w3.to_wei(amount_eth, "ether")

        params = {
            "sellToken": "ETH",
            "buyToken": token_address,
            "sellAmount": str(amount_wei),
            "takerAddress": settings.chain.wallet_address,
            "chainId": settings.chain.chain_id,
        }
        quote = await self._get_quote(params)

        # buyAmount is in the token's smallest unit; 18 decimals is the common
        # case on Base but NOT universal — verify per-token via its contract
        # if you see obviously-wrong entry prices logged (a strong sign of a
        # decimals mismatch, e.g. USDC-style 6-decimal tokens).
        buy_amount_raw = float(quote.get("buyAmount", 0))
        amount_tokens = buy_amount_raw / (10 ** 18)
        entry_price_usd = (amount_usd / amount_tokens) if amount_tokens else 0.0

        if settings.dry_run:
            log.info(f"[DRY RUN] Would BUY {token_address} for ~${amount_usd:.2f} "
                      f"({amount_eth:.6f} ETH). Quote price: {quote.get('price')}")
            return {
                "dry_run": True, "quote": quote,
                "amount_tokens": amount_tokens, "entry_price_usd": entry_price_usd,
            }

        send_result = await self._send_swap(quote)
        send_result["amount_tokens"] = amount_tokens
        send_result["entry_price_usd"] = entry_price_usd
        return send_result

    async def sell(self, token_address: str, amount_tokens: float) -> dict:
        """Sell `amount_tokens` of `token_address` back to ETH."""
        amount_wei = int(amount_tokens * (10 ** 18))  # assumes 18 decimals; verify per-token

        params = {
            "sellToken": token_address,
            "buyToken": "ETH",
            "sellAmount": str(amount_wei),
            "takerAddress": settings.chain.wallet_address,
            "chainId": settings.chain.chain_id,
        }
        quote = await self._get_quote(params)

        if settings.dry_run:
            log.info(f"[DRY RUN] Would SELL {amount_tokens} of {token_address}. "
                      f"Quote: {quote.get('price')}")
            return {"dry_run": True, "quote": quote}

        return await self._send_swap(quote)

    async def _get_quote(self, params: dict) -> dict:
        headers = {"0x-api-key": settings.chain.zerox_api_key} if settings.chain.zerox_api_key else {}
        async with httpx.AsyncClient() as client:
            resp = await client.get(ZEROX_QUOTE_URL, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.json()

    async def _send_swap(self, quote: dict) -> dict:
        if self.account is None:
            raise RuntimeError("No wallet configured — set WALLET_PRIVATE_KEY in .env")

        tx = {
            "to": Web3.to_checksum_address(quote["to"]),
            "data": quote["data"],
            "value": int(quote.get("value", 0)),
            "gas": int(quote.get("gas", 300000)),
            "gasPrice": int(quote.get("gasPrice", self.w3.eth.gas_price)),
            "nonce": self.w3.eth.get_transaction_count(self.account.address),
            "chainId": settings.chain.chain_id,
        }
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info(f"Sent tx: {tx_hash.hex()}")
        return {"dry_run": False, "tx_hash": tx_hash.hex()}

    async def _get_eth_price_usd(self) -> float:
        if not WETH_ROBINHOOD:
            log.warning(
                "WETH_ADDRESS not set in .env — using fallback ETH price estimate. "
                "Set the verified Robinhood Chain WETH address for accurate sizing."
            )
            return 3000.0
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.dexscreener.com/latest/dex/tokens/" + WETH_ROBINHOOD, timeout=10
            )
            data = resp.json()
            pairs = data.get("pairs") or []
            if pairs:
                return float(pairs[0]["priceUsd"])
            return 3000.0  # crude fallback if API is unreachable
