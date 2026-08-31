"""
Portfolio tracker — the trade log the risk manager doesn't keep.

RiskManager only knows about OPEN positions (it deletes a position the
moment it's closed). This module is the permanent record: every time a
position is fully closed, record_trade() appends it here, and get_stats()
/ format_pnl_card_html() turn that log into "how many Xs have I actually
done" and a shareable P&L summary.

Usage:
    from portfolio.tracker import PortfolioTracker
    portfolio = PortfolioTracker()
    portfolio.record_trade(position, exit_price_usd=..., pnl_usd=..., reason=...)
    print(portfolio.format_summary_text())

Can also be run directly for a quick check from the command line:
    python -m portfolio.tracker
"""
import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional


@dataclass
class ClosedTrade:
    token_address: str
    token_symbol: str
    entry_price_usd: float
    exit_price_usd: float
    cost_basis_usd: float      # what was actually still deployed at close (remaining_amount_usd)
    exit_value_usd: float
    pnl_usd: float
    multiple: float            # exit_price / entry_price — the "X" achieved
    opened_at: str
    closed_at: str
    reason: str
    source_channel: str = ""


class PortfolioTracker:
    def __init__(self, state_path: str = "logs/portfolio_state.json"):
        self.state_path = state_path
        self.trades: list[ClosedTrade] = []
        self._load_state()

    # ---------- persistence ----------
    def _load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                data = json.load(f)
            self.trades = [ClosedTrade(**t) for t in data.get("trades", [])]

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({"trades": [asdict(t) for t in self.trades]}, f, indent=2)

    # ---------- recording ----------
    def record_trade(self, position, exit_price_usd: float, pnl_usd: float, reason: str) -> ClosedTrade:
        """`position` is a risk.manager.Position — already popped from
        RiskManager.positions by the time close_position() returns, so pass
        the object you held onto before calling close_position()."""
        trade = ClosedTrade(
            token_address=position.token_address,
            token_symbol=position.token_symbol,
            entry_price_usd=position.entry_price_usd,
            exit_price_usd=exit_price_usd,
            cost_basis_usd=position.remaining_amount_usd,
            exit_value_usd=position.remaining_amount_usd + pnl_usd,
            pnl_usd=pnl_usd,
            multiple=exit_price_usd / position.entry_price_usd if position.entry_price_usd else 0.0,
            opened_at=position.opened_at,
            closed_at=datetime.utcnow().isoformat(),
            reason=reason,
            source_channel=position.source_channel,
        )
        self.trades.append(trade)
        self._save_state()
        return trade

    # ---------- stats ----------
    def get_stats(self) -> dict:
        if not self.trades:
            return {
                "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "total_pnl_usd": 0.0, "total_deployed_usd": 0.0, "total_pnl_pct": 0.0,
                "avg_multiple": 0.0, "best_trade": None, "worst_trade": None,
                "x_buckets": {},
            }

        wins = [t for t in self.trades if t.pnl_usd > 0]
        losses = [t for t in self.trades if t.pnl_usd <= 0]
        total_pnl = sum(t.pnl_usd for t in self.trades)
        total_deployed = sum(t.cost_basis_usd for t in self.trades)
        best = max(self.trades, key=lambda t: t.multiple)
        worst = min(self.trades, key=lambda t: t.multiple)

        # Bucket by X achieved — mirrors the tp_probability_tiers bands plus
        # a bucket for anything that closed below entry (stop-loss / a miss
        # that later rolled over into a loss).
        buckets = {"<1x (loss)": 0, "1x-3x": 0, "3x-5x": 0, "5x-10x": 0, "10x+": 0}
        for t in self.trades:
            m = t.multiple
            if m < 1.0:
                buckets["<1x (loss)"] += 1
            elif m < 3.0:
                buckets["1x-3x"] += 1
            elif m < 5.0:
                buckets["3x-5x"] += 1
            elif m < 10.0:
                buckets["5x-10x"] += 1
            else:
                buckets["10x+"] += 1

        return {
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(self.trades),
            "total_pnl_usd": total_pnl,
            "total_deployed_usd": total_deployed,
            "total_pnl_pct": (total_pnl / total_deployed) if total_deployed else 0.0,
            "avg_multiple": sum(t.multiple for t in self.trades) / len(self.trades),
            "best_trade": best,
            "worst_trade": worst,
            "x_buckets": buckets,
        }

    # ---------- formatting ----------
    def format_summary_text(self) -> str:
        s = self.get_stats()
        if s["total_trades"] == 0:
            return "No closed trades yet."

        lines = [
            f"Portfolio — {s['total_trades']} closed trades",
            f"Win rate: {s['win_rate']:.0%} ({s['wins']}W / {s['losses']}L)",
            f"Total P&L: ${s['total_pnl_usd']:.2f} ({s['total_pnl_pct']:+.1%} on ${s['total_deployed_usd']:.2f} deployed)",
            f"Avg multiple: {s['avg_multiple']:.2f}x",
            f"Best: {s['best_trade'].token_symbol} @ {s['best_trade'].multiple:.2f}x (${s['best_trade'].pnl_usd:.2f})",
            f"Worst: {s['worst_trade'].token_symbol} @ {s['worst_trade'].multiple:.2f}x (${s['worst_trade'].pnl_usd:.2f})",
            "X distribution:",
        ]
        for bucket, count in s["x_buckets"].items():
            if count:
                lines.append(f"  {bucket}: {count}")
        return "\n".join(lines)

    def format_pnl_card_html(self) -> str:
        """HTML-formatted for the Telegram notifier (parse_mode=HTML)."""
        s = self.get_stats()
        if s["total_trades"] == 0:
            return "📊 <b>Portfolio</b>\nNo closed trades yet."

        pnl_emoji = "🟢" if s["total_pnl_usd"] >= 0 else "🔴"
        lines = [
            "📊 <b>Portfolio update</b>",
            f"Trades: {s['total_trades']}  |  Win rate: {s['win_rate']:.0%} ({s['wins']}W/{s['losses']}L)",
            f"P&L: {pnl_emoji} ${s['total_pnl_usd']:.2f} ({s['total_pnl_pct']:+.1%})",
            f"Avg multiple: {s['avg_multiple']:.2f}x  |  Best: {s['best_trade'].multiple:.2f}x",
            "",
            "<b>X distribution:</b>",
        ]
        for bucket, count in s["x_buckets"].items():
            if count:
                lines.append(f"  {bucket}: {count}")
        return "\n".join(lines)


if __name__ == "__main__":
    tracker = PortfolioTracker()
    print(tracker.format_summary_text())
