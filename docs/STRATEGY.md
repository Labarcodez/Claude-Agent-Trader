# Strategy, risk, and coin fundamentals

This is the reference the `trade-cycle` skill leans on for *why*, not just
*what*. Read this before changing `config/risk.yaml`, adding to
`config/watchlist.yaml`, or trusting a new strategy live.

## The core problem with a $50 account

Fees and slippage are a fixed-ish cost per trade; on a $50 account they're a
much bigger percentage bite than on a $50,000 account. A strategy that
"backtests great" trading every few hours will bleed to death on fees at this
size. That's why `config/risk.yaml` caps daily trade count/volume and prefers
fewer, larger, higher-conviction trades. Every strategy choice below is
filtered through: **does the edge survive realistic Solana swap costs at
$10-20 position sizes?** `backtest/engine.py` charges 30bps fee + 50bps
slippage by default specifically to stress-test that.

## Coin fundamentals -- what actually matters

When looking at any token (watchlisted or a new candidate), these are the
signals that matter, roughly in order:

1. **Liquidity** (depth of the on-chain trading pool, e.g. on Raydium/Orca/
   Jupiter routes). Low liquidity means large price impact on even small
   trades, and means a rug/exit-scam can drain the pool instantly. This repo
   hard-gates on `min_liquidity_usd` for a reason.
2. **Volume vs. market cap**. Extremely high volume relative to market cap
   can mean wash trading (fake volume to look legitimate) -- be suspicious of
   volume that's a large multiple of market cap sustained over days.
3. **Mint & freeze authority** (Solana-specific). A token where the deployer
   still holds mint authority can print unlimited new supply and dump on
   holders; freeze authority lets them freeze your tokens. Reputable,
   established tokens (SOL, USDC, JUP, JTO, PYTH, RAY) have these
   renounced/controlled appropriately. Check via a Solana explorer or RPC
   `getAccountInfo` on the mint before ever adding a new token to the
   watchlist.
4. **Holder concentration**. If a handful of wallets hold a large majority
   of supply, a single wallet dumping can crater price. Check top-holder
   distribution before adding a new token.
5. **Age & track record**. A token with months/years of continuous trading
   history and multiple market cycles survived is safer than a token that
   launched last week, all else equal -- new tokens are where most rugs and
   pump-and-dumps happen.
6. **What the token actually is**. Understand the project: is it a
   real-usage token (a DEX's governance token, an oracle network, a staking
   protocol) or a pure attention/meme token with no underlying activity?
   Both can be traded, but meme tokens get smaller position-size multipliers
   in `config/watchlist.yaml` because their liquidity and price can evaporate
   far faster.

### Due-diligence checklist (must pass ALL before adding a token to the watchlist)

- [ ] Liquidity >= `min_liquidity_usd` sustained over the last 7+ days, not a spike
- [ ] Volume/market-cap ratio not wildly anomalous vs. comparable tokens
- [ ] Mint authority renounced or held by a known, reputable program/multisig
- [ ] Freeze authority renounced (or acceptable for a known reason, e.g. a
      regulated stablecoin issuer)
- [ ] Top 10 non-exchange, non-liquidity-pool holders don't control an
      outsized share of supply
- [ ] Token/project has a real, checkable identity (website, docs, audits if
      a DeFi protocol) -- not just a ticker and a Telegram group
- [ ] Mint address verified against an authoritative source (the project's
      own docs/site, or Jupiter's token list), not just copied from a chat or
      search result -- **impersonator tokens with similar names/tickers are
      extremely common on Solana**

A token that fails any box gets logged as a rejected proposal in the journal,
not added to the watchlist, and never traded -- see `config/risk.yaml`'s
`require_watchlist_membership`.

### Meme coin handling

Meme tokens can be added to the watchlist if they clear the checklist above,
but always at a reduced `category_position_fraction_multiplier` (0.4x by
default in `config/watchlist.yaml`) because liquidity that looks fine today
can vanish in hours. Re-verify liquidity every single cycle for meme-category
holdings, not just at watchlist-add time.

## Current strategies (see `backtest/strategies.py`)

- **`sma_crossover`** -- trend-following. Buys when a fast moving average
  crosses above a slow one, sells on the reverse cross. Good in trending
  markets, whipsaws (many small losing trades) in sideways/choppy markets.
- **`rsi_mean_reversion`** -- buys oversold conditions, sells overbought.
  Good in range-bound markets, fights strong trends (can keep "buying the
  dip" through a real downtrend).
- **`volatility_breakout`** -- buys new highs with confirming volatility,
  sells new lows. Momentum-continuation; sensitive to lookback tuning and
  false breakouts.

None of these is inherently "the" strategy -- they suit different market
regimes. That's why `trade-cycle` requires combining signals conservatively
(step 4) rather than blindly trusting one.

### Judging a backtest

- Compare against simple buy-and-hold SOL over the same window as a
  baseline. Complexity has to earn its keep.
- Total return alone is misleading -- weigh it against max drawdown (can the
  $50 account's circuit breaker survive that path?) and win rate/trade count
  (very few trades means the result isn't statistically meaningful; re-test
  over a longer window).
- Backtests use daily closes from CoinGecko, not intraday OHLC -- treat
  results as directional evidence of an edge, not a precise forecast of live
  P&L. Live fills will differ.
- Re-run backtests periodically (see `.claude/skills/backtest-strategy`).
  Markets regime-shift; a strategy validated in a trending quarter can fail
  in a choppy one.

## Position sizing philosophy

Fixed-fractional sizing (a % of *current* portfolio value, not a fixed $
amount) means the account naturally compounds winners into slightly larger
future position sizes and shrinks after losses -- this is deliberate ("keep
building" from a small base) but is capped hard by `max_position_usd` so a
lucky streak doesn't concentrate the whole account into one token.

## Exit discipline

`stop_loss_pct`, `take_profit_pct`, and `trailing_stop_pct` in
`config/risk.yaml` apply per-position regardless of what the entry strategy
was -- a strategy's job is to decide entries; risk management (this file's
rules, enforced by `trade-cycle`) decides exits independent of whether the
original signal has "changed its mind" yet. This protects against a strategy
that's slow to signal an exit in a fast-moving loss.
