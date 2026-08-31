"""
Risk manager — the single gatekeeper that decides whether a trade is allowed
to happen at all, and when an open position must be trimmed or closed.

Nothing in execution/ should place a trade without going through here first.

Exit strategy, in order of precedence on every price check:
  1. Hard stop-loss (50% below entry) -> close the ENTIRE remaining position.
  2. Probabilistic take-profit -> the first time price enters a new tier band
     (2x-3x, 3x-5x, 5x+), roll once against that tier's base probability,
     nudged by recent momentum. A "hit" closes the ENTIRE remaining position.
     A "miss" is a one-time decision to hold through that band — the position
     falls through to the trailing stop below for protection, and can still
     roll again if/when it reaches the next band up.
  3. Momentum-aware trailing stop -> once the position is up, a stop trails
     below the highest price seen, and how far it trails TIGHTENS as the
     multiple grows (wide early, tighter once a position is already a big
     winner, so a stall after a huge run still locks in most of the gain).
"""
import json
import os
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional

from config import settings


@dataclass
class Position:
    token_address: str
    token_symbol: str
    entry_price_usd: float
    original_amount_usd: float     # size at open, never changes — basis for scale-out %s
    remaining_amount_usd: float    # current USD value still held at entry-cost-basis
    amount_tokens: float           # current token quantity still held
    opened_at: str
    highest_price_usd: float
    source_channel: str
    levels_taken: list = field(default_factory=list)  # legacy — unused, kept for old state files
    tp_tiers_rolled: list = field(default_factory=list)  # indices into tp_probability_tiers already rolled

    @property
    def current_multiple(self) -> float:
        return self.highest_price_usd / self.entry_price_usd if self.entry_price_usd else 0.0


class RiskManager:
    def __init__(self, state_path: str = "logs/risk_state.json"):
        self.state_path = state_path
        self.positions: dict[str, Position] = {}
        self.daily_loss_usd: float = 0.0
        self.daily_reset_date: str = date.today().isoformat()
        self._load_state()

    # ---------- persistence ----------
    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                data = json.load(f)
            self.positions = {
                k: Position(**v) for k, v in data.get("positions", {}).items()
            }
            self.daily_loss_usd = data.get("daily_loss_usd", 0.0)
            self.daily_reset_date = data.get("daily_reset_date", date.today().isoformat())
        self._maybe_reset_daily()

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({
                "positions": {k: asdict(v) for k, v in self.positions.items()},
                "daily_loss_usd": self.daily_loss_usd,
                "daily_reset_date": self.daily_reset_date,
            }, f, indent=2)

    def _maybe_reset_daily(self):
        today = date.today().isoformat()
        if today != self.daily_reset_date:
            self.daily_loss_usd = 0.0
            self.daily_reset_date = today
            self._save_state()

    # ---------- pre-trade checks ----------
    def can_open_position(self) -> tuple[bool, str]:
        """Returns (allowed, reason_if_not)."""
        self._maybe_reset_daily()

        if self.daily_loss_usd >= settings.risk.max_daily_loss_usd:
            return False, (
                f"Daily loss limit hit (${self.daily_loss_usd:.2f} >= "
                f"${settings.risk.max_daily_loss_usd:.2f}). No more trades today."
            )

        if len(self.positions) >= settings.risk.max_concurrent_positions:
            return False, (
                f"Max concurrent positions reached "
                f"({len(self.positions)}/{settings.risk.max_concurrent_positions})."
            )

        deployed = sum(p.remaining_amount_usd for p in self.positions.values())
        remaining = settings.risk.total_bankroll_usd - deployed
        if remaining < settings.risk.min_position_usd:
            return False, (
                f"Not enough free bankroll (${remaining:.2f} left, "
                f"need >= ${settings.risk.min_position_usd:.2f})."
            )

        return True, ""

    def position_size_usd(self) -> float:
        deployed = sum(p.remaining_amount_usd for p in self.positions.values())
        remaining = settings.risk.total_bankroll_usd - deployed
        return max(
            settings.risk.min_position_usd,
            min(settings.risk.max_position_usd, remaining)
        )

    # ---------- position lifecycle ----------
    def open_position(self, token_address: str, token_symbol: str,
                       entry_price_usd: float, amount_usd: float,
                       amount_tokens: float, source_channel: str) -> Position:
        pos = Position(
            token_address=token_address,
            token_symbol=token_symbol,
            entry_price_usd=entry_price_usd,
            original_amount_usd=amount_usd,
            remaining_amount_usd=amount_usd,
            amount_tokens=amount_tokens,
            opened_at=datetime.utcnow().isoformat(),
            highest_price_usd=entry_price_usd,
            source_channel=source_channel,
            levels_taken=[],
        )
        self.positions[token_address] = pos
        self._save_state()
        return pos

    def _record_realized_pnl(self, sold_cost_basis_usd: float, sold_value_usd: float):
        pnl = sold_value_usd - sold_cost_basis_usd
        if pnl < 0:
            self.daily_loss_usd += abs(pnl)

    def close_position(self, token_address: str, exit_price_usd: float) -> Optional[float]:
        """Fully closes whatever remains of a position. Returns realized P&L in USD."""
        pos = self.positions.pop(token_address, None)
        if pos is None:
            return None
        exit_value = pos.amount_tokens * exit_price_usd
        self._record_realized_pnl(pos.remaining_amount_usd, exit_value)
        pnl = exit_value - pos.remaining_amount_usd
        self._save_state()
        return pnl

    def scale_out(self, token_address: str, current_price_usd: float,
                   fraction_of_original: float) -> Optional[dict]:
        """
        LEGACY / not currently called by check_exit_conditions (which now
        does probabilistic full closes instead of fixed partial trims).
        Kept in case a future strategy wants partial trims again — sells
        `fraction_of_original` of the ORIGINAL position size (not of
        whatever remains). Updates remaining size/tokens accordingly.
        Returns details of the trim, or None if the position doesn't exist
        or there's nothing left to sell at that fraction.
        """
        pos = self.positions.get(token_address)
        if pos is None:
            return None

        sell_amount_usd_basis = pos.original_amount_usd * fraction_of_original
        sell_amount_usd_basis = min(sell_amount_usd_basis, pos.remaining_amount_usd)
        if sell_amount_usd_basis <= 0:
            return None

        fraction_of_remaining = sell_amount_usd_basis / pos.remaining_amount_usd
        tokens_to_sell = pos.amount_tokens * fraction_of_remaining
        sell_value_usd = tokens_to_sell * current_price_usd

        pos.remaining_amount_usd -= sell_amount_usd_basis
        pos.amount_tokens -= tokens_to_sell
        self._record_realized_pnl(sell_amount_usd_basis, sell_value_usd)

        if pos.remaining_amount_usd <= 0.01 or pos.amount_tokens <= 0:
            self.positions.pop(token_address, None)
        self._save_state()

        return {
            "tokens_sold": tokens_to_sell,
            "value_usd": sell_value_usd,
            "cost_basis_usd": sell_amount_usd_basis,
            "position_closed": token_address not in self.positions,
        }

    # ---------- exit logic ----------
    def check_exit_conditions(self, token_address: str, current_price_usd: float,
                               momentum_score: float = 0.0) -> Optional[dict]:
        """
        Call this on every price update for an open position.

        momentum_score: recent price momentum, clamped to [-1, 1]. Positive =
        still trending up, negative = stalling/falling. Only affects the
        probabilistic take-profit roll below — pass 0.0 if unavailable.

        Returns a dict describing the action to take, or None if no action
        needed: {"action": "close_all", "reason": ...}
        The caller (main loop) is responsible for actually executing the sell
        and then calling close_position() to update state.
        """
        pos = self.positions.get(token_address)
        if pos is None:
            return None

        if current_price_usd > pos.highest_price_usd:
            pos.highest_price_usd = current_price_usd
            self._save_state()

        current_multiple = current_price_usd / pos.entry_price_usd

        # 1. Hard stop-loss takes priority over everything
        drawdown = (pos.entry_price_usd - current_price_usd) / pos.entry_price_usd
        if drawdown >= settings.risk.stop_loss_pct:
            return {"action": "close_all", "reason": f"stop_loss triggered ({drawdown:.1%} below entry)"}

        # 2. Probabilistic take-profit — roll ONCE the first time price enters
        # a new tier band. A hit closes everything; a miss is a one-time
        # decision to hold, and control falls through to the trailing stop.
        momentum_score = max(-1.0, min(1.0, momentum_score))
        for i, (lo, hi, base_prob) in enumerate(settings.risk.tp_probability_tiers):
            in_band = current_multiple >= lo and (hi is None or current_multiple < hi)
            if in_band and i not in pos.tp_tiers_rolled:
                pos.tp_tiers_rolled.append(i)
                adjusted_prob = max(0.05, min(0.95,
                    base_prob - momentum_score * settings.risk.momentum_influence))
                roll = random.random()
                self._save_state()
                band_label = f"{lo}x-{hi}x" if hi is not None else f"{lo}x+"
                if roll < adjusted_prob:
                    return {
                        "action": "close_all",
                        "reason": (
                            f"tp_probabilistic close at {current_multiple:.2f}x "
                            f"(tier {band_label}, {adjusted_prob:.0%} chance, "
                            f"momentum={momentum_score:+.2f}, roll={roll:.2f})"
                        ),
                    }
                else:
                    log_msg = (
                        f"tp roll missed at {current_multiple:.2f}x (tier {band_label}, "
                        f"{adjusted_prob:.0%} chance, roll={roll:.2f}) — holding, "
                        f"trailing stop now protects it"
                    )
                    import logging
                    logging.getLogger("risk_manager").info(f"[{pos.token_symbol}] {log_msg}")
                break  # only one band can be newly entered per price check

        # 3. Momentum-aware trailing stop, using the highest tier reached so far
        applicable_trail_pct = settings.risk.trailing_stop_tiers[0][1]
        for min_multiple, trail_pct in sorted(settings.risk.trailing_stop_tiers):
            if pos.current_multiple >= min_multiple:
                applicable_trail_pct = trail_pct

        gain_from_high = (pos.highest_price_usd - current_price_usd) / pos.highest_price_usd
        if pos.highest_price_usd > pos.entry_price_usd and gain_from_high >= applicable_trail_pct:
            return {
                "action": "close_all",
                "reason": (
                    f"trailing_stop triggered ({gain_from_high:.1%} below high, "
                    f"tier trail={applicable_trail_pct:.0%} at {pos.current_multiple:.1f}x peak)"
                ),
            }

        return None
