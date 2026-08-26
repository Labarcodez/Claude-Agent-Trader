# Strategy, risk, and how to actually make money on Kraken

This is the reference the `trade-cycle` skill leans on for *why*, not just
*what*. Read this before changing `config/risk.yaml`, `config/discovery.yaml`,
or trusting a new strategy live.

## The core problem: fees are real on a CEX

Kraken charges an explicit maker/taker fee on every trade -- 0.25%/0.40% at
the lowest (Starter) 30-day-volume tier, improving with volume (as low as
0%/0.05%+ at the top tiers). That's a real, disclosed cost, unlike the
project's original DEX-routed Solana version, which could (mostly)
free-ride on Jupiter's routing. Fees and spread are a fixed-ish cost per
trade; on a small account they're a much bigger percentage bite than on a
large one. A strategy that "backtests great" trading every few hours will
bleed to death on fees at small size. That's why `config/risk.yaml` caps
daily trade count/volume and prefers fewer, larger, higher-conviction
trades. Every strategy choice below is filtered through: **does the edge
survive realistic Kraken fees/spread at small position sizes?**
`backtest/engine.py` charges 40bps fee (Kraken's lowest-tier taker rate) +
20bps slippage by default specifically to stress-test that.

## Fee-aware execution

This is the single highest-leverage, purely mechanical lever this project
has for "make more money on Kraken" -- it costs nothing to implement and
compounds on every trade:

- **Prefer maker (limit) orders over taker (market) orders on entries.**
  Kraken's maker fee is materially lower than taker (0.25% vs 0.40% at the
  lowest tier -- a ~15bps edge on every entry, which is not small relative
  to this project's own `stop_loss_pct`/`take_profit_pct` bands).
  `config/risk.yaml`'s `prefer_maker_orders: true` encodes this; see
  `.claude/skills/trade-cycle/SKILL.md` step 8. Use a market/taker order
  only when the signal is genuinely time-critical (a stop-loss exit should
  never wait on a limit order that might not fill).
- **Fee tier compounds with account activity.** Kraken's July 2026 change
  lets an account qualify for a better tier via either 30-day trade volume
  OR assets-on-platform, whichever is better -- meaning a small account that
  mostly holds (rather than churns) can sometimes land a better rate than
  volume alone would suggest. Worth re-checking (`kraken volume --pair
  <pair>`) periodically rather than assuming the lowest tier forever.
  `config/risk.yaml`'s `max_taker_fee_bps` should be re-verified against
  whatever tier the account is actually on.
- **Check spread and order-book depth before sizing a market order.** A
  wide spread or thin book means the effective cost is much higher than the
  fee alone -- `config/risk.yaml`'s `max_spread_bps` and
  `min_orderbook_depth_usd` exist for exactly this, checked live at
  execution time (`kraken orderbook`), not just from the last discovery
  pass a few minutes ago.
- **Fewer, larger, higher-conviction trades beat frequent small ones** on a
  small account, for the same reason as the original Solana version, just
  with real disclosed numbers behind it now instead of an assumed-near-zero
  DEX cost.

## Idle-capital yield (Kraken Earn)

This is a genuine "make more money" lever the project's original Solana
version didn't have at all -- Phantom/Jupiter has no equivalent native
yield product. Cash sitting idle between trades (no open position, or the
regime filter blocking new entries) earns literally 0% just sitting in a
balance. Kraken's Earn program lets it work instead.

**The one rule that makes this safe rather than a trap:** only ever
allocate to **flexible** Earn products (unstake any time, no on-chain
unbonding wait). Kraken's **Bonded Earn** products pay a materially higher
rate specifically *because* they lock capital for 3-28+ days depending on
the asset -- which is fine for capital you'll never need on short notice,
and actively dangerous for capital a circuit breaker or stop-loss might
need to reach in the next cycle. `config/risk.yaml`'s
`asset_classes.kraken_earn.flexible_only: true` is a hard rule, not a
preference, for exactly this reason -- see that file's comment and
`.claude/skills/trade-cycle/SKILL.md` step 9.

In practice: after sizing this cycle's trades, whatever cash remains above
`earn_allocate_idle_cash_above_usd` (a configurable trade-ready buffer, not
all of it) is a candidate for flexible-Earn allocation. Deallocate before
any circuit-breaker response, withdrawal, or trade that needs the cash --
allocated Earn balance doesn't count as "available" until it's actually
back in the spendable balance.

## What actually matters when evaluating a pair

When looking at any Kraken pair, discovered or pinned, these are the
signals that matter, roughly in order -- and each one maps directly to an
automated check in `research/discover_candidates.py` (see "Autonomous
discovery" below for exactly how):

1. **24h quote volume**. Low volume means large spread and unpredictable
   slippage on even modest trades. This repo hard-gates on
   `min_24h_quote_volume_usd` for a reason -- and unlike an on-chain
   liquidity-pool number, this is Kraken's own live order-flow data, not
   something that can be faked by a wash-trading pool.
2. **Bid/ask spread**. The single most direct measure of what a round-trip
   actually costs beyond the stated fee. A pair with a wide spread can look
   "liquid" by 24h volume alone while still being expensive to actually
   trade -- `max_spread_bps` catches this independently of the volume check.
3. **Kraken's own listing status**. Unlike a permissionless DEX, every
   Kraken-listed pair has already gone through Kraken's own compliance and
   listing review -- there's no mint/freeze-authority or holder-concentration
   check to run, because the exchange itself is the trust boundary. What
   still needs a live check is the pair's *current* `status` field
   (`online` vs. `cancel_only`/`post_only`/`limit_only`/`reduce_only`/
   `delisted`/etc.) -- a pair can go into a restricted state without being
   fully delisted.
4. **Not a leveraged/ETP product**. A small set of exchange-listed products
   (e.g. a 3x-long/3x-short token) are not a spot position in the
   underlying asset at all -- they decay from daily rebalancing and aren't
   what this project's strategies/backtests are validated against.
   `leveraged_token_denylist_pattern` in `config/discovery.yaml` excludes
   these by symbol suffix as a safety net, independent of the volume/spread
   checks.
5. **What the pair actually is**. A large-cap major (BTC, ETH) and a
   smaller-cap alt can both be traded -- what differs is how they're sized,
   via the tier system below, not whether they're eligible at all.

## Autonomous discovery

There is no hand-maintained pair list in this project. `research/discover_candidates.py`
runs live, every cycle (or reuses a recent run within `config/risk.yaml`'s
`max_discovery_result_age_hours`), and does two things: **finds** every
pair Kraken currently lists, and **filters** them down to only the ones
that pass automated liquidity/spread/status checks. Nothing is tradeable
unless it clears the filter -- Kraken listing a pair is necessary, not
sufficient.

### Where candidates come from

Both endpoints are Kraken's own public REST API (`api.kraken.com/0/public`),
free, no key required:
- `AssetPairs` -- every pair Kraken lists, with status, quote currency,
  order-size minimums, and margin availability.
- `Ticker` -- live 24h volume, vwap, and top-of-book bid/ask per pair,
  fetched in batches for whatever `AssetPairs` returned.

Config: `config/discovery.yaml`'s `sources` block.

### What gets checked, and why this design

Unlike the project's original Solana/Jupiter pipeline, there's no
multi-stage "cheap check first, expensive check only on survivors"
structure here, because there's no expensive third-party check needed at
all -- Kraken's own two public endpoints already carry everything relevant
(status, quote currency, order minimums, live volume, live spread) at zero
extra cost beyond the batched `Ticker` calls themselves. The two-endpoint
design (`AssetPairs` for static pair metadata, `Ticker` for live market
data) mirrors why the on-chain version needed Jupiter *and* RugCheck: one
source doesn't have the whole picture, but here both sources are Kraken's
own, so there's no cross-source trust question the way "does Jupiter's data
agree with RugCheck's" was for the old pipeline.

Thresholds live in `config/discovery.yaml`'s `safety` block and must be
kept in sync with `research/discover_candidates.py`'s CLI defaults (the
script doesn't parse the YAML, to stay dependency-free -- see the top of
that file).

### Risk tiers (computed live, never hand-labeled)

`config/discovery.yaml`'s `tiers` block classifies every eligible candidate
from its own live Kraken data -- 24h quote volume and spread -- into
`blue_chip`, `established`, or `emerging`, each with its own position
sizing multiplier (`trade-cycle` step 7). This is a deliberate change from
the original version's market-cap-based tiering: Kraken's own volume/spread
numbers are a more directly relevant proxy for "can this exchange actually
absorb my order size" than an off-exchange market-cap figure would be, and
they don't require a separate lookup to get.

**`emerging` is the catch-all for anything that clears every safety check
above but isn't blue_chip/established-liquid yet.** They are in scope on
purpose: excluding them would give up real, faster-moving edge. What
actually needs managing is that they behave differently than majors
(liquidity can thin out faster, spread can widen faster under stress),
which is why sizing is smaller and `max_emerging_tier_positions` caps
concurrent exposure -- not because they're restricted on principle, but
because their volatility and tail risk per dollar is genuinely higher,
which the volatility-scaled sizing formula below also captures
quantitatively.

Note that BTC and ETH are **not** hardcoded as always-blue-chip the way the
original version hardcoded SOL as a core/exempt asset (see
`config/core_assets.yaml`) -- they'll clear `blue_chip` on their own live
volume/spread data essentially every cycle, so there's no need for a
special case, and letting them earn it from real data is strictly more
correct.

`config/discovery.yaml`'s `pinned_candidates` (always considered, but still
has to pass every safety check -- pinning isn't a bypass) and `denylist`
(never traded, no exceptions) are the two manual overrides left in the
system, for a human to use deliberately rather than by default.

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
and `trade-cycle` step 6's conservative-combination rule are trying to
manage rather than ignore. This strategy math is entirely asset-agnostic
(pure price-series functions) and carried over from the project's original
version unchanged -- what changed is the universe it runs on (Kraken pairs
instead of Solana tokens) and the fee/slippage assumptions it's validated
against (see "The core problem" above).

### Judging a backtest

- **Run `backtest/backtest_all.py` periodically**, not just one-off
  single-pair runs -- it backtests everything currently eligible (plus
  BTC/ETH) in one pass and is what surfaces cross-asset patterns like the
  tier-risk one above. A single pair's backtest can look fine in isolation
  while missing that the *strategy itself* is currently underwater across
  most of the live universe.
- **Run `--walk-forward`, not just an in-sample run.** A strategy (or
  hand-tuned parameters) that only performs on the exact window it was
  fitted to is fitting noise, not finding an edge. `backtest/run_backtest.py
  --walk-forward` reports train vs. test separately and flags a large gap as
  overfit risk -- treat `HIGH` as disqualifying until investigated, not as a
  number to argue past.
- Compare against simple buy-and-hold BTC over the same window as a
  baseline. Complexity has to earn its keep.
- Total return alone is misleading -- weigh it against max drawdown (can
  the account's circuit breaker survive that path?) and win rate/trade
  count (very few trades means the result isn't statistically meaningful;
  re-test over a longer window).
- Backtests use daily closes (Kraken OHLC or CoinGecko), not intraday order
  flow -- treat results as directional evidence of an edge, not a precise
  forecast of live P&L. Live fills, spread, and fee-tier will differ.
- Re-run backtests periodically (see `.claude/skills/backtest-strategy`).
  Markets regime-shift; a strategy validated in a trending quarter can fail
  in a choppy one -- this is also what the live regime filter (below) is
  compensating for in real time, not just at backtest time.

## Position sizing philosophy

Fixed-fractional sizing (a % of *current* portfolio value, not a fixed $
amount) means the account naturally compounds winners into slightly larger
future position sizes and shrinks after losses -- this is deliberate ("keep
building" from a small base) but is capped hard by `max_position_usd` so a
lucky streak doesn't concentrate the whole account into one pair.

### Volatility-scaled sizing

A flat fraction of the account sized the same into a calm major and a
volatile small-cap alt is implicitly taking on far more risk in the alt per
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
`regime_reference_pair` (`XBTUSD` by default, as a broad crypto risk
barometer) is currently trading above its own `regime_sma_window_days`-day
SMA. The idea: most alt setups that look good on a chart still get dragged
down in a broad market downturn, so it's not worth opening *new* risk while
the overall tape is deteriorating, even if a specific pair's own signal
says buy. Existing positions still get managed by their own
stop-loss/take-profit regardless of regime state.

### Concentration guardrails

`max_emerging_tier_positions`, `max_same_ecosystem_positions`, and
`max_non_stable_exposure_fraction` in `config/risk.yaml` exist because
several nominally-different pairs can be the same bet in disguise (e.g.
three different L1 tokens that all just move with BTC beta, or two DeFi
governance tokens that both move on the same sector sentiment).
Diversification across tickers isn't real diversification if they're all
correlated to the same underlying factor.

## Exit discipline

`stop_loss_pct`, `take_profit_pct`, and `trailing_stop_pct` in
`config/risk.yaml` apply per-position regardless of what the entry strategy
was -- a strategy's job is to decide entries; risk management (this file's
rules, enforced by `trade-cycle`) decides exits independent of whether the
original signal has "changed its mind" yet. This protects against a
strategy that's slow to signal an exit in a fast-moving loss. Where
practical, place these as Kraken's own native `stop-loss`/`take-profit`/
`trailing-stop` order types rather than only checking them in software each
cycle -- a resting order on Kraken survives even if a future trade-cycle
run is missed or delayed, which pure software-side checking cannot
guarantee.

## What this project deliberately doesn't do (yet)

Kraken offers more than spot: margin trading (up to ~10x on many pairs,
20x for eligible US retail on some), perpetual futures (up to 50x), and
tokenized-stock/forex products. All are available through kraken-cli, none
are enabled by default (`config/risk.yaml`'s `asset_classes`). Leverage
multiplies both edge and liquidation risk, and this project's entire risk
model (position sizing, circuit breaker, portfolio heat) is built and
validated for unleveraged spot -- turning on margin/futures without
re-deriving those numbers for a leveraged context would silently invalidate
every risk cap in this file. Revisit only after the spot strategy has a
real live track record, and treat it as a deliberate, separate decision,
not a config flag to flip casually.
