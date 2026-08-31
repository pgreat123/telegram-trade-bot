"""
Background loop that polls DexScreener for the current price of every open
position and asks the risk manager whether action is needed (scale-out,
trailing-stop close, or hard stop-loss close), then executes it.

This is what makes stop-loss / scale-out / trailing-stop actually DO
something — without this running, check_exit_conditions() in the risk
manager is never called and positions just sit there un-managed.
"""
import asyncio
import logging

import httpx

from config import settings
from risk.manager import RiskManager
from execution.executor import Executor
from notifier.telegram_notifier import notify, notify_position_closed
from portfolio.tracker import PortfolioTracker

log = logging.getLogger("exit_monitor")

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
POLL_INTERVAL_SECONDS = 30


async def get_price_and_momentum(token_address: str, client: httpx.AsyncClient) -> tuple[float, float] | tuple[None, None]:
    """Returns (price_usd, momentum_score) or (None, None) on failure.

    momentum_score is built from DexScreener's own 5m/1h % price-change
    fields (weighted 60/40 toward the more recent one), scaled and clamped
    to [-1, 1]. This is a cheap proxy — no extra price history needs to be
    stored — and it's the same signal DexScreener already surfaces to
    describe whether a token is still trending up or stalling.
    """
    try:
        resp = await client.get(DEXSCREENER_URL.format(address=token_address), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        base_pairs = [p for p in pairs if p.get("chainId") == "robinhood"] or pairs
        if not base_pairs:
            return None, None
        best = max(base_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        price = float(best["priceUsd"])

        change = best.get("priceChange", {}) or {}
        m5 = float(change.get("m5", 0) or 0) / 100.0
        h1 = float(change.get("h1", 0) or 0) / 100.0
        # 10% move in the relevant window maps to a full +/-1.0 momentum score
        raw = (m5 * 0.6 + h1 * 0.4) / 0.10
        momentum = max(-1.0, min(1.0, raw))
        return price, momentum
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        log.warning(f"Price fetch failed for {token_address}: {e}")
        return None, None


async def run_exit_monitor(risk_manager: RiskManager, executor: Executor,
                            portfolio: PortfolioTracker | None = None):
    portfolio = portfolio or PortfolioTracker()
    log.info(f"Exit monitor started, polling every {POLL_INTERVAL_SECONDS}s.")
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            # snapshot to avoid mutating dict while iterating
            open_tokens = list(risk_manager.positions.keys())
            if not open_tokens:
                continue

            for token_address in open_tokens:
                price, momentum = await get_price_and_momentum(token_address, client)
                if price is None:
                    continue

                decision = risk_manager.check_exit_conditions(token_address, price, momentum_score=momentum)
                if decision is None:
                    continue

                pos = risk_manager.positions.get(token_address)
                symbol = pos.token_symbol if pos else token_address[:10]

                # Only close_all decisions exist now — the old fixed-fraction
                # scale-out was replaced by the probabilistic take-profit tiers.
                if decision["action"] == "close_all":
                    log.info(f"[{symbol}] CLOSING full position: {decision['reason']}")
                    pos_before = risk_manager.positions.get(token_address)
                    if pos_before is None:
                        continue
                    result = await executor.sell(token_address, pos_before.amount_tokens)
                    dry_run = bool(result.get("dry_run"))
                    if not dry_run:
                        pnl = risk_manager.close_position(token_address, price)
                        log.info(f"[{symbol}] Closed. Realized P&L: ${pnl:.2f}")
                        portfolio.record_trade(pos_before, exit_price_usd=price,
                                                pnl_usd=pnl, reason=decision["reason"])
                        await notify_position_closed(symbol, decision["reason"], pnl, dry_run=False)
                        await notify(portfolio.format_pnl_card_html())
                    else:
                        log.info(f"[{symbol}] (dry run — position kept open in state for testing)")
                        await notify_position_closed(symbol, decision["reason"], None, dry_run=True)
