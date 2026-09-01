"""
Central configuration for the trading bot.
All secrets are loaded from environment variables (.env file) — never hardcoded.
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TelegramConfig:
    api_id: int = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash: str = os.getenv("TELEGRAM_API_HASH", "")
    # Comma-separated list of channel usernames or invite links in .env
    # e.g. TELEGRAM_CHANNELS=ca_thesis_channel,live_buys_channel
    channels: list = field(default_factory=lambda: [
        c.strip() for c in os.getenv("TELEGRAM_CHANNELS", "").split(",") if c.strip()
    ])
    session_name: str = os.getenv("TELEGRAM_SESSION_NAME", "trade_bot_session")
    session_string: str = os.getenv("TELEGRAM_SESSION_STRING", "")

@dataclass
class ChainConfig:
    chain: str = "robinhood"
    rpc_url: str = os.getenv("ROBINHOOD_CHAIN_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
    chain_id: int = 4663
    wallet_private_key: str = os.getenv("WALLET_PRIVATE_KEY", "")
    wallet_address: str = os.getenv("WALLET_ADDRESS", "")
    # 0x API is used for swap routing/quotes — confirmed day-1 support for Robinhood Chain
    zerox_api_key: str = os.getenv("ZEROX_API_KEY", "")
    explorer_url: str = "https://robinhoodchain.blockscout.com"
    # WETH on Robinhood Chain — verify against docs.robinhood.com/chain/protocol-contracts
    # yourself before setting this. Do not trust an address from Telegram/search/chat.
    weth_address: str = os.getenv("WETH_ADDRESS", "")


@dataclass
class RiskConfig:
    total_bankroll_usd: float = 50.0
    min_position_usd: float = 5.0
    max_position_usd: float = 10.0
    stop_loss_pct: float = 0.50       # 50% drawdown from entry triggers full exit
    max_daily_loss_usd: float = 15.0  # halts all trading for the day once hit
    max_concurrent_positions: int = 3

    # Probabilistic take-profit tiers: (min_multiple, max_multiple_or_None, base_probability)
    # Each time price enters a new band for the FIRST time, the bot rolls once:
    # with probability `base_probability` it closes the FULL remaining position;
    # otherwise it holds and falls back to the trailing stop / stop-loss below.
    # base_probability is nudged by recent momentum (see momentum_influence) —
    # strong upward momentum lowers the close chance (let it ride), stalling/
    # negative momentum raises it (lock in profit before it round-trips).
    tp_probability_tiers: list = field(default_factory=lambda: [
        (2.0, 3.0, 0.50),
        (3.0, 5.0, 0.30),
        (5.0, None, 0.20),
    ])
    # How much recent momentum can swing base_probability, in percentage points.
    # momentum_score is clamped to [-1, 1]; adjusted = base - momentum * this value.
    momentum_influence: float = 0.15

    # Momentum-aware trailing stop: as the position's gain multiple increases,
    # the trailing stop tightens (protects more of a big move) but never so
    # tight that ordinary volatility knocks you out early.
    # (min_multiple_of_entry, trailing_stop_pct_from_high)
    trailing_stop_tiers: list = field(default_factory=lambda: [
        (0.0, 0.50),    # below entry to 1x gain: wide 50% trail (~matches stop-loss)
        (2.0, 0.35),    # once 2x+: trail 35% off the high
        (5.0, 0.30),    # once 5x+: trail 30% off the high
        (10.0, 0.25),   # once 10x+: trail 25% off the high
        (50.0, 0.20),   # once 50x+: trail 20% off the high — lock in the moonshot
    ])


@dataclass
class NotifierConfig:
    # A separate BOT account (from @BotFather) — not your personal userbot
    # session used by the listener. See notifier/telegram_notifier.py for
    # setup steps. Both must be set for DMs to actually send; if either is
    # missing, notifications are silently skipped and the bot runs as normal.
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


@dataclass
class SafetyConfig:
    min_liquidity_usd: float = float(os.getenv("MIN_LIQUIDITY_USD", "15000"))  # skip tokens with thinner pools than this
    max_holder_concentration_pct: float = 0.30  # skip if top wallet holds >30% of supply
    require_sell_check: bool = True        # simulate a sell before buying (honeypot check)
    require_renounced_or_locked: bool = True  # deployer must have renounced or locked liquidity
    min_token_age_minutes: int = 0         # 0 = allow brand new tokens (risky, but that's the game)


@dataclass
class Settings:
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    chain: ChainConfig = field(default_factory=ChainConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    log_dir: str = "logs"


settings = Settings()
