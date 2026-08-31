# Telegram-signal Robinhood Chain trading bot

Watches two Telegram channels (a "CA + thesis" channel and a "live buys / X-multiples"
channel), parses trade calls, runs safety checks, and auto-executes swaps on
**Robinhood Chain** (Robinhood's Ethereum L2, chain ID 4663) via a funded wallet —
with a risk manager enforcing position size, a 50% stop-loss, tiered profit-taking,
and a momentum-aware trailing stop that lets winners run.

## ⚠️ Before you touch real money

- This bankroll is $50. That is genuinely small enough that a single bad trade,
  gas-fee drain, or bug can wipe a meaningful chunk of it. Treat it as tuition.
- **Start in DRY_RUN mode** (the default). Let it log real signals and simulated
  decisions for at least several days before flipping `DRY_RUN=false`.
- The safety checks (liquidity, honeypot/sellability) catch a lot of obvious scams
  but not all of them. Sophisticated rugs can pass a sell-simulation check and still
  drain later. No automated check is a substitute for the channels themselves being
  trustworthy — if the calls are consistently bad, no amount of code fixes that.
- Robinhood Chain specifically has a documented, active ecosystem of fake token
  contracts (fake WETH/USDC clones, lookalike RPC/explorer domains). Before setting
  `WETH_ADDRESS` in `.env`, verify it yourself at
  https://docs.robinhood.com/chain/protocol-contracts — never trust an address from
  a Telegram post, a search result, or anywhere in this codebase's comments.
- Holder-concentration checking is stubbed out (`safety/checks.py` has a TODO) —
  needs a Robinhood Chain-compatible block explorer API (Blockscout at
  robinhoodchain.blockscout.com has one) to implement properly.
- A realistic expectation check: targeting huge multiples is fine as a strategy,
  but "100Mx" isn't something to size your risk around — even extreme historical
  meme-coin runs have topped out far below that. The scale-out/trailing-stop setup
  below is built to let a real runner go as far as it will, not to promise it will.

## Setup

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill in:
   - `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org
   - `TELEGRAM_CHANNELS` — comma-separated usernames/links for your two channels
   - `WALLET_PRIVATE_KEY` / `WALLET_ADDRESS` — your Robinhood Chain wallet
     (⚠️ never share this key with anyone, including in chat with me — only put it
     in this local `.env` file)
   - `WETH_ADDRESS` — verify against Robinhood's own docs first (see warning above)
   - Optionally `ZEROX_API_KEY` for higher rate limits on the 0x swap API
3. First run: `python main.py` — Telethon will prompt for your phone number and a
   login code the first time, then save a session file so you don't log in again.

## Project layout

```
config.py              - all settings, loaded from .env
listener/               - Telegram client, watches configured channels
parser/                 - extracts contract addresses / calls from raw messages
safety/                 - liquidity + honeypot pre-trade checks
risk/                   - position sizing, stop-loss, probabilistic take-profit, trailing-stop, state
portfolio/              - closed-trade log + stats (win rate, avg X, P&L, X distribution)
execution/              - swap execution on Robinhood Chain via 0x API, exit-monitoring loop
main.py                 - wires it all together
logs/signals.jsonl      - append-only log of every signal and what happened to it
logs/risk_state.json    - open positions (risk/manager.py)
logs/portfolio_state.json - closed trades (portfolio/tracker.py)
```

## Exit strategy (risk/manager.py)

On every price check, in order:
1. **Hard stop-loss** — 50% below entry closes the full remaining position.
2. **Probabilistic take-profit** — the first time price enters a new tier band, the
   bot rolls once against that tier's base chance of a **full** close (not a partial
   trim): 2x-3x → 50%, 3x-5x → 30%, 5x+ → 20%. The chance is nudged by recent
   momentum (DexScreener's 5m/1h % change) — still trending up lowers the close
   chance, stalling/falling raises it. A miss is a one-time decision to hold through
   that band; the position falls through to the trailing stop for protection, and can
   still roll again once it reaches the next band up. Tiers/probabilities live in
   `config.py` (`RiskConfig.tp_probability_tiers`, `momentum_influence`).
3. **Momentum-aware trailing stop** — protects whatever remains, tightening as the
   position's multiple grows: 50% trail below 2x, 35% at 2x+, 30% at 5x+, 25% at
   10x+, 20% at 50x+. A stall after a big run gets locked in tighter than normal
   volatility near entry would.

`execution/exit_monitor.py` polls DexScreener every 30s for every open position and
executes whatever the risk manager decides.

## Portfolio tracker (portfolio/tracker.py)

Every full close gets appended to `logs/portfolio_state.json` with its entry/exit
price, the X multiple achieved, and realized P&L. After a live close, the bot also
DMs you an updated P&L card via the notifier. You can check it anytime without
waiting for a trade:

```
python -m portfolio.tracker
```

which prints trade count, win rate, total P&L (in $ and % of capital deployed),
average X, best/worst trade, and a bucketed X distribution (<1x, 1x-2x, 2x-3x,
3x-5x, 5x+) — i.e. "how many Xs have I actually done."

## What's NOT yet wired up (next steps)

- **Holder concentration check**: stubbed, needs a block explorer API integration.
- **Matching "Xs done" updates back to earlier calls**: the parser returns these
  as standalone events; nothing yet links "3x" back to which token it refers to.
- **Per-token decimals**: buy/sell amount math currently assumes 18 decimals for
  all tokens, which is common but not universal on EVM chains. Worth hardening if
  you see obviously-wrong entry prices logged.

Want to tackle any of these next?
