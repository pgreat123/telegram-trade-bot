"""
Main entry point — wires the pipeline together:

  Telegram message -> parse signal -> safety checks -> risk manager -> execute

Run with:  python main.py
Stop with: Ctrl+C

Starts in DRY_RUN mode by default (see .env) — no real trades until you
flip that flag after reviewing the logs.
"""
import asyncio
import logging
import os
from datetime import datetime

from config import settings
from listener.telegram_listener import run_listener
from safety.checks import run_safety_checks
from risk.manager import RiskManager
from execution.executor import Executor
from execution.exit_monitor import run_exit_monitor
from notifier.telegram_notifier import notify_buy_executed, notify_blocked
from portfolio.tracker import PortfolioTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("main")

risk_manager = RiskManager()
executor = Executor()
portfolio = PortfolioTracker()

os.makedirs(settings.log_dir, exist_ok=True)
SIGNAL_LOG_PATH = os.path.join(settings.log_dir, "signals.jsonl")


def log_signal_event(signal, decision: str, extra: dict | None = None):
    import json
    entry = {
        "time": datetime.utcnow().isoformat(),
        "signal": signal.__dict__,
        "decision": decision,
        "extra": extra or {},
    }
    with open(SIGNAL_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


async def handle_signal(signal):
    if signal.action == "update":
        log.info(f"Performance update: {signal.x_multiple}x from {signal.source_channel}")
        log_signal_event(signal, "logged_update")
        return

    if signal.action != "buy":
        log.info(f"Ignoring non-buy signal: {signal.action}")
        log_signal_event(signal, "ignored_non_buy")
        return

    if not signal.has_thesis and signal.source_channel:
        # A bare CA with no context at all is the lowest-confidence case.
        # Still allowed through, but flagged — tighten this later once you
        # have data on which calls actually perform.
        log.info("Note: signal has no accompanying thesis/context — lower confidence")

    # 1. Risk manager: can we even open a new position right now?
    allowed, reason = risk_manager.can_open_position()
    if not allowed:
        log.warning(f"Trade blocked by risk manager: {reason}")
        log_signal_event(signal, "blocked_by_risk_manager", {"reason": reason})
        await notify_blocked(signal.token_address, "risk", reason)
        return

    # 2. Safety checks: liquidity, sellability (honeypot), etc.
    safety_result = await run_safety_checks(signal.token_address)
    if not safety_result.passed:
        log.warning(f"Trade blocked by safety check: {safety_result.reason}")
        log_signal_event(signal, "blocked_by_safety_check", {"reason": safety_result.reason})
        await notify_blocked(signal.token_address, "safety", safety_result.reason)
        return

    # 3. Size the position and execute
    amount_usd = risk_manager.position_size_usd()
    log.info(f"Executing BUY: {signal.token_address} for ${amount_usd:.2f} "
             f"(liquidity=${safety_result.liquidity_usd:,.0f})")

    result = await executor.buy(signal.token_address, amount_usd)
    log_signal_event(signal, "executed_buy", {
        "amount_usd": amount_usd,
        "liquidity_usd": safety_result.liquidity_usd,
        "dry_run": result.get("dry_run"),
        "entry_price_usd": result.get("entry_price_usd"),
    })
    await notify_buy_executed(
        signal.token_address, amount_usd, safety_result.liquidity_usd,
        dry_run=bool(result.get("dry_run")),
    )

    if not result.get("dry_run"):
        risk_manager.open_position(
            token_address=signal.token_address,
            token_symbol=signal.token_address[:10],  # symbol lookup can be added later
            entry_price_usd=result["entry_price_usd"],
            amount_usd=amount_usd,
            amount_tokens=result["amount_tokens"],
            source_channel=signal.source_channel,
        )
        log.info(f"Position opened: entry=${result['entry_price_usd']:.8f}, "
                 f"tokens={result['amount_tokens']:.4f}")
    else:
        log.info("Dry run — position NOT recorded in risk manager "
                 "(nothing to track an exit against yet).")


async def main():
    mode = "DRY RUN (no real trades)" if settings.dry_run else "LIVE TRADING"
    log.info(f"Starting bot in {mode} mode.")
    await asyncio.gather(
        run_listener(handle_signal),
        run_exit_monitor(risk_manager, executor, portfolio),
    )


if __name__ == "__main__":
    asyncio.run(main())
