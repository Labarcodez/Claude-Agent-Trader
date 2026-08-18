# Strategy, risk, and coin fundamentals

This is the reference the `trade-cycle` skill leans on for *why*, not just
*what*. Read this before changing `config/risk.yaml`, `config/discovery.yaml`,
or trusting a new strategy live.

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

When looking at any token, discovered or pinned, these are the signals that
matter, roughly in order -- and each one maps directly to an automated check
in `research/discover_candidates.py` (see "Autonomous discovery" below for
exactly how):

1. **Liquidity** (depth of the on-chain trading pool, e.g. on Raydium/Orca/
   Jupiter routes). Low liquidity means large price impact on even small
   trades, and means a rug/exit-scam can drain the pool instantly. This repo
   hard-gates on `min_liquidity_usd` for a reason.
2. **Real vs. wash-traded volume**. High volume relative to market cap can
   mean wash trading (fake volume to look legitimate). Jupiter's `organicScore`
   -- built by filtering out bot/wash-trade patterns -- is a much sharper tool
   for this than eyeballing a volume/mcap ratio.
3. **Mint & freeze authority** (Solana-specific). A token where the deployer
   still holds mint authority can print unlimited new supply and dump on
   holders; freeze authority lets them freeze your tokens. This is a hard,
   automated gate (`audit.mintAuthorityDisabled` / `audit.freezeAuthorityDisabled`
   from Jupiter's Tokens API) -- it isn't optional even for a token that
   otherwise looks great, and notably it isn't just a memecoin problem: see
   the worked example below, where the pipeline correctly rejected USDT for
   exactly this reason.
4. **Holder concentration**. If a handful of wallets hold a large majority
   of supply, a single wallet dumping can crater price. Automated via
   Jupiter's `audit.topHoldersPercentage`.
5. **Age & track record**. A token whose pool has survived days/weeks/months
   is safer than one that launched hours ago, all else equal -- most rugs and
   pump-and-dumps happen in the first hours to days. Automated via
   `firstPool.createdAt`.
6. **What the token actually is**. A real-usage token (a DEX's governance
   token, an oracle network) and a pure attention/meme token can both be
   traded -- what differs is how they're sized, via the tier system below,
   not whether they're eligible at all.

## Autonomous discovery

There is no hand-maintained token list in this project. `config/watchlist.yaml`
existed early on and was deliberately removed -- a static list either goes
stale (missing real opportunities) or becomes a bottleneck requiring a human
to add every token by hand, which defeats the point of an autonomous agent.
Instead, `research/discover_candidates.py` runs live, every cycle (or reuses
a recent run within `config/risk.yaml`'s `max_discovery_result_age_hours`),
and does two things: **finds** candidates worth looking at, and **filters**
them down to only the ones that pass automated, on-chain-backed safety
checks. Nothing is tradeable unless it clears the filter -- discovery finding
a token is necessary, not sufficient.

### Where candidates come from

All via [Jupiter's Tokens API v2](https://dev.jup.ag/docs/tokens/v2) (`api.jup.ag/tokens/v2`,
free, no key):
- `toporganicscore/{6h,24h}` -- tokens with real (non-wash-traded) trading interest
- `toptrending/{1h,6h}` -- momentum/attention signal
- `recent` -- newest pools, by first-pool creation time (highest-risk source,
  balanced by the pool-age gate below)

Config: `config/discovery.yaml`'s `sources` block.

### What gets checked, and why this design

The single most useful discovery here: **Jupiter's own token response
already includes an `audit` block** (`mintAuthorityDisabled`,
`freezeAuthorityDisabled`, `topHoldersPercentage`) plus `isVerified`,
`organicScore`, and `firstPool.createdAt` -- so most of the due-diligence
checklist above is answerable from data the discovery call already returned,
at zero extra network cost. The pipeline is two-staged specifically to take
advantage of that:

1. **Stage 1 (free, from the discovery response itself)**: liquidity,
   holder count, organic score, mint/freeze authority, top-holder
   concentration, pool age -- all computed with no extra API calls.
2. **Stage 2 ([RugCheck.xyz](https://api.rugcheck.xyz), free, no key)**:
   called *only* for candidates that already survive stage 1, specifically
   for the one thing Jupiter's own data doesn't cover -- has this exact mint
   already been confirmed as a rug (`report.rugged`), and does it carry any
   RugCheck risk flag at danger/critical/high level.

This ordering matters for a reason beyond efficiency: it was tested, not
assumed. Querying RugCheck for a brand-new, 5-holder pump.fun token with a
single wallet holding 99.96% of supply and effectively zero real liquidity
returned `score: 1` (RugCheck's own composite 1-100 risk score, where lower
reads as *safer*) and an **empty** `risks` array -- i.e., RugCheck's
headline score alone would have called that token safe. The actual red
flags were visible in *other* fields of the same response (holder
concentration, liquidity), just not folded into the composite score. That's
why this pipeline never trusts a single composite score from any one source
-- every individual signal (liquidity, holders, concentration, age, organic
score, authorities) is checked explicitly, and RugCheck's role is narrowed
to what it's uniquely good for (confirmed rug history), not treated as a
one-number verdict.

An optional third check (`--cross-check-dexscreener`, on by default, only
run against candidates that already pass) queries DexScreener as an
independent liquidity/pool cross-check -- defense in depth against any
single source drifting or being gamed.

Thresholds live in `config/discovery.yaml`'s `safety` block and must be kept
in sync with `research/discover_candidates.py`'s CLI defaults (the script
doesn't parse the YAML, to stay dependency-free -- see the top of that file).

### A real worked example

A live run against Solana mainnet (see the script's own output) evaluated 12
freshly-discovered candidates and found:

- **`USDT` rejected** -- "mint authority not disabled; freeze authority not
  disabled." Correct: Tether retains admin mint/freeze control over USDT by
  design. This is exactly the kind of check that matters regardless of
  whether a token *feels* like an obvious blue-chip.
- Several pump.fun-style tokens rejected for **top-holder concentration above
  20%** (one at 62.8%, one at 69.3%) -- classic single-wallet rug setups.
- Several rejected for **pool age under 72 hours** and/or **liquidity under
  $250k** -- too new/thin to trust yet, independent of anything else about them.
- One token (`neet`, an established pump.fun-launched token) passed every
  check: $1.05M liquidity, 20,808 holders, organic score 79.3, mint/freeze
  authority both disabled, pool well past the age floor -- classified
  `tier: established`.

This is what "the agent finds its own coins" means concretely: not vibes or
an LLM's judgment call on a mint address, but a repeatable, auditable filter
run fresh against live data every cycle.

### Risk tiers (computed live, never hand-labeled)

`config/discovery.yaml`'s `tiers` block classifies every eligible candidate
from its own live data -- market cap, holder count, verification status --
into `blue_chip`, `established`, or `emerging`, each with its own position
sizing multiplier (`trade-cycle` step 7). **`emerging` is the catch-all for
anything that clears every safety check but isn't large/established yet --
this is where memecoins land.** They are in scope on purpose: excluding them
would give up real, fast-moving edge. What actually needs managing is that
they fail differently than blue-chips (liquidity can be gone in hours, not
weeks; impersonator tokens reusing a popular name/ticker are common), which
is why sizing is smaller and `max_emerging_tier_positions` caps concurrent
exposure -- not because they're restricted on principle, but because their
volatility and tail risk per dollar is genuinely higher, which the
volatility-scaled sizing formula below also captures quantitatively.

`config/discovery.yaml`'s `pinned_candidates` (always considered, but still
has to pass every safety check -- pinning isn't a bypass) and `denylist`
(never traded, no exceptions) are the two manual overrides left in the
system, for a human to use deliberately rather than by default.

**This isn't a theoretical justification** -- `backtest/backtest_all.py`
(see "Judging a backtest" below) backtests SOL/BTC alongside whatever's
currently discovery-eligible, and a real run turned up exactly the pattern
the tier system exists for: two emerging-tier tokens both showed *far*
larger best-case out-of-sample returns than SOL/BTC in the same window
(+68% and +27%) but with drawdowns of -50% to -70% along the way, vs. -9%
to -10% for SOL/BTC over the same period. Bigger edge and bigger tail risk,
simultaneously, on the same tokens -- which is precisely why emerging-tier
sizing is smaller rather than either "excluded" or "sized the same as a
blue-chip."

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

- **Run `backtest/backtest_all.py` periodically**, not just one-off
  single-token runs -- it backtests everything currently eligible (plus
  SOL/BTC) in one pass and is what surfaces cross-asset patterns like the
  tier-risk one above. A single token's backtest can look fine in isolation
  while missing that the *strategy itself* is currently underwater across
  most of the live universe.
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

`max_emerging_tier_positions`, `max_same_ecosystem_positions`, and
`max_non_stable_exposure_fraction` in `config/risk.yaml` exist because
several nominally-different tokens can be the same bet in disguise (e.g.
three different Solana-DeFi governance tokens that all just move with SOL
beta, or two memecoins that both move on the same broad meme-market
sentiment). Diversification across tickers isn't real diversification if
they're all correlated to the same underlying factor.

## Exit discipline

`stop_loss_pct`, `take_profit_pct`, and `trailing_stop_pct` in
`config/risk.yaml` apply per-position regardless of what the entry strategy
was -- a strategy's job is to decide entries; risk management (this file's
rules, enforced by `trade-cycle`) decides exits independent of whether the
original signal has "changed its mind" yet. This protects against a strategy
that's slow to signal an exit in a fast-moving loss.
