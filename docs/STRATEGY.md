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

Meme coins are **in scope, on purpose** -- the agent can trade any
watchlisted, verified token that looks like it can make money, memes
included. Excluding them on principle would give up real, fast-moving edge;
what actually needs managing is that meme tokens fail differently than
blue-chips, not that they shouldn't be traded at all:

- Liquidity that looks fine today can be gone in hours, not weeks -- the
  checklist isn't a one-time gate for memes, re-run the liquidity/volume
  checks (and ideally holder concentration) every single cycle, not just at
  watchlist-add time.
- Impersonator tokens reusing a popular ticker/name are extremely common.
  Mint-address verification (see the checklist above) matters *more* here,
  not less -- a wrong mint on a meme coin is a much easier mistake to make
  than on an established blue-chip, precisely because there are more
  copies floating around.
- Price action is attention/momentum-driven more than fundamentals-driven.
  Discovery tools (e.g. CoinGecko `get-trending`) are a legitimate way to
  surface meme candidates worth researching -- but discovery only ever
  *proposes*; it never trades directly (`trade-cycle` step 4).
- Sizing stays smaller (`category_position_fraction_multiplier.meme` in
  `config/watchlist.yaml`, plus `max_meme_positions` in `config/risk.yaml`)
  because the *volatility and tail risk per dollar* is higher, which the
  volatility-scaled sizing formula below already captures quantitatively --
  the category multiplier is a second, simpler backstop on top of that, not
  a redundant restriction dressed up as caution.

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
- **`adaptive_ensemble`** -- regime-aware weighted vote across the three
  above: mostly trend-following signals when `regime()` detects a real trend
  (wide fast/slow SMA gap relative to volatility), mostly RSI mean-reversion
  when it detects chop. This is the default candidate for live use once it
  clears the bar in "Judging a backtest" below -- not because it's
  guaranteed better, but because picking one fixed strategy means betting
  the whole account on the market staying in the regime that strategy likes.

None of the three base strategies is inherently "the" strategy -- they suit
different market regimes, which is exactly the problem `adaptive_ensemble`
and `trade-cycle` step 5's conservative-combination rule are trying to
manage rather than ignore.

### Judging a backtest

- **Run `--walk-forward`, not just an in-sample run.** A strategy (or
  hand-tuned parameters) that only performs on the exact window it was
  fitted to is fitting noise, not finding an edge. `backtest/run_backtest.py
  --walk-forward` reports train vs. test separately and flags a large gap as
  overfit risk -- treat `HIGH` as disqualifying until investigated, not as a
  number to argue past.
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
  in a choppy one -- this is also what the live regime filter (below) is
  compensating for in real time, not just at backtest time.

## Position sizing philosophy

Fixed-fractional sizing (a % of *current* portfolio value, not a fixed $
amount) means the account naturally compounds winners into slightly larger
future position sizes and shrinks after losses -- this is deliberate ("keep
building" from a small base) but is capped hard by `max_position_usd` so a
lucky streak doesn't concentrate the whole account into one token.

### Volatility-scaled sizing

A flat fraction of the account sized the same into a calm blue-chip and a
wild meme coin is implicitly taking on far more risk in the meme coin per
dollar deployed. `config/risk.yaml`'s `target_daily_volatility_pct` +
`volatility_size_min_mult`/`max_mult` scale the base position size down for
higher-volatility assets and up (within a cap) for lower-volatility ones, so
a given trade's *actual risk contribution* stays closer to constant across
very different assets. See `backtest/strategies.py`'s `realized_vol()` for
the reference calculation the agent should replicate live.

### Portfolio heat

Per-position stop-losses cap the damage from any *one* position going wrong.
They don't, by themselves, cap what happens if several correlated positions
all hit their stops on the same bad day -- which is exactly when it
happens, since crypto correlations move toward 1 in a broad selloff.
`max_portfolio_heat_pct` in `config/risk.yaml` caps
`sum(position_size_usd * stop_loss_pct) / portfolio_value_usd` across *all*
open positions combined, so the worst-case simultaneous-stop-out loss stays
bounded independent of how many individually-small positions are open.

### Market regime filter

`regime_filter_enabled` gates **new entries only** (never exits) on whether
`regime_reference_coin` (BTC by default, as a broad crypto risk barometer)
is currently trading above its own `regime_sma_window_days`-day SMA. The
idea: most alt/meme setups that look good on a chart still get dragged down
in a broad market downturn, so it's not worth opening *new* risk while the
overall tape is deteriorating, even if a specific token's own signal says
buy. Existing positions still get managed by their own stop-loss/take-profit
regardless of regime state.

### Concentration guardrails

`max_meme_positions`, `max_same_ecosystem_positions`, and
`max_non_stable_exposure_fraction` in `config/risk.yaml` exist because
several nominally-different tokens can be the same bet in disguise (e.g.
three different Solana-DeFi governance tokens that all just move with SOL
beta, or two meme coins that both move on the same broad meme-market
sentiment). Diversification across tickers isn't real diversification if
they're all correlated to the same underlying factor.

## Exit discipline

`stop_loss_pct`, `take_profit_pct`, and `trailing_stop_pct` in
`config/risk.yaml` apply per-position regardless of what the entry strategy
was -- a strategy's job is to decide entries; risk management (this file's
rules, enforced by `trade-cycle`) decides exits independent of whether the
original signal has "changed its mind" yet. This protects against a strategy
that's slow to signal an exit in a fast-moving loss.
