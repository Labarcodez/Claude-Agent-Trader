# Strategy, risk, and asset fundamentals

This is the reference the `trade-cycle` skill leans on for *why*, not just
*what*. Read this before changing `config/risk.yaml`, `config/discovery.yaml`,
or trusting a new strategy live.

(This project traded Solana meme/altcoins via Jupiter + Phantom MCP before
2026-08-26 -- some of this file's reasoning below carries over unchanged
[position sizing, exits, backtest judgment], some was rewritten for Kraken's
different safety model [autonomous discovery]. See `CLAUDE.md`'s header for
what changed.)

## The core problem with a small account

Fees and slippage are a fixed-ish cost per trade; on a small account they're
a much bigger percentage bite than on a large one. A strategy that
"backtests great" trading every few hours will bleed to death on fees at
this size. That's why `config/risk.yaml` caps daily trade count/volume and
prefers fewer, larger, higher-conviction trades. Every strategy choice below
is filtered through: **does the edge survive realistic Kraken trading costs
at small position sizes?** `backtest/engine.py` charges 26bps fee (Kraken's
non-VIP taker rate) + 20bps slippage by default specifically to stress-test
that.

## Asset fundamentals -- what actually matters

When looking at any pair, discovered or pinned, these are the signals that
matter -- each one maps directly to an automated check in
`research/discover_candidates.py` (see "Autonomous discovery" below):

1. **24h volume** (exit-liquidity proxy). Low volume means you may not be
   able to exit a position at a reasonable price when you want to -- this
   repo hard-gates on `min_24h_volume_usd` for exactly that reason.
2. **Bid/ask spread**. A wide spread means entering and exiting the same
   position hands away real money to the spread alone, independent of
   whether the strategy's signal was right. Gated on `max_spread_bps`.
3. **Listing status**. Kraken's own `AssetPairs` status field
   (`online`/`cancel_only`/`post_only`/etc.) is the exchange-level signal
   for "is this actually tradable right now" -- the closest analog to the
   old Solana pipeline's mint/freeze-authority check, just enforced by
   Kraken's own listing review instead of on-chain data this project reads
   itself.
4. **Market cap and volume together** (tier). Both must clear a tier's bar
   (`blue_chip`/`established`) for a pair to size up -- see "Risk tiers"
   below. A pair that clears one but not the other stays `emerging`.

**What's different from a DEX-listed token, and why the old checks don't
carry over 1:1**: mint/freeze authority, RugCheck rug-history, and holder
concentration were all about verifying an arbitrary on-chain contract wasn't
a scam, because *anyone* can deploy a Solana token. Kraken only lists assets
after its own review, and a centralized exchange account can't have its
underlying asset "rug pulled" by a deployer the way an unaudited on-chain
contract can -- the risk that remains is liquidity/tradability, not
contract-level fraud. That's the whole reason the safety model changed
shape rather than just being ported over with new field names.

## Autonomous discovery

There is no hand-maintained pair list in this project. `research/discover_candidates.py`
runs live, every cycle (or reuses a recent run within `config/risk.yaml`'s
`max_discovery_result_age_hours`), and does two things: **finds** every
online USD pair Kraken lists, and **filters** them down to only the ones
that pass automated liquidity/spread checks. Nothing is tradeable unless it
clears the filter -- discovery finding a pair is necessary, not sufficient.

### Where candidates come from

Two bulk calls to Kraken's public REST API (`kraken/client.py`, no key
needed):
- `AssetPairs` -- every pair Kraken lists, with its live tradability
  `status` and base/quote asset codes
- `Ticker` -- live bid/ask/last price and 24h volume for every pair

Unlike the old Jupiter-based pipeline (several momentum-snapshot sources
that each surfaced a different slice of a much larger, faster-moving token
universe), Kraken's full USD-pair universe is a few hundred pairs, already
deduped, evaluated in full every cycle -- there's no sampling/rotation
concept needed (see `research/discover_candidates.py`'s module docstring).

Config: `config/discovery.yaml`'s `sources` block.

### What gets checked, and why this design

Both checks are computed entirely from the two bulk calls above -- no
per-candidate secondary lookup (contrast the old pipeline's per-candidate
RugCheck call):

1. **`min_24h_volume_usd`** -- the exit-liquidity proxy described above.
2. **`max_spread_bps`** -- the bid/ask spread as a fraction of mid price.
3. **`status == "online"`** -- Kraken's own tradability signal.

Market cap (for tiering, not eligibility) comes from a third source --
CoinGecko's public markets endpoint, looked up in one bulk call for a small
curated list of Kraken-listed majors (`KRAKEN_TO_COINGECKO_ID` in
`research/discover_candidates.py`) -- since Kraken's own API has no
market-cap field at all.

Thresholds live in `config/discovery.yaml`'s `safety` block and must be kept
in sync with `research/discover_candidates.py`'s CLI defaults (the script
doesn't parse the YAML, to stay dependency-free -- see the top of that file).

### Risk tiers (computed live, never hand-labeled)

`config/discovery.yaml`'s `tiers` block classifies every eligible candidate
from its own live data -- market cap AND 24h volume, both required -- into
`blue_chip`, `established`, or `emerging`, each with its own position sizing
multiplier (`trade-cycle` step 7). **`emerging` is the catch-all for
anything that clears every safety check but isn't large/liquid yet.** These
are in scope on purpose: excluding them would give up real, fast-moving
edge. What actually needs managing is that they fail differently than
blue-chips (a thinner order book moves more on the same-size trade, and a
sudden liquidity/volume drop can happen faster), which is why sizing is
smaller and `max_emerging_tier_positions` caps concurrent exposure -- not
because they're restricted on principle, but because their volatility and
tail risk per dollar is genuinely higher, which the volatility-scaled sizing
formula below also captures quantitatively.

`config/discovery.yaml`'s `pinned_candidates` (always considered, but still
has to pass every safety check -- pinning isn't a bypass) and `denylist`
(never traded, no exceptions) are the two manual overrides left in the
system, for a human to use deliberately rather than by default.

**This isn't a theoretical justification** -- `backtest/backtest_all.py`
(see "Judging a backtest" below) backtests BTC/ETH alongside whatever's
currently discovery-eligible, and a real run turned up exactly the pattern
the tier system exists for: emerging-tier pairs (e.g. a couple of
lower-cap tokens both showing 80%+ best-case out-of-sample returns) posted
*far* larger best-case returns than BTC/ETH in the same window, but also
carried the deeper drawdowns typical of thinner order books -- bigger edge
and bigger tail risk simultaneously, on the same pairs, which is precisely
why emerging-tier sizing is smaller rather than either "excluded" or "sized
the same as a blue-chip." Re-run `backtest/backtest_all.py` periodically to
see the current live version of this pattern -- it changes as the eligible
universe changes.

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
- **`adaptive_ensemble_fast`** -- same ensemble logic as `adaptive_ensemble`
  but with roughly half the lookback windows (5/15-bar SMA, 7-bar RSI,
  10-bar breakout), on the hypothesis that the original's windows -- sized
  for slower assets like BTC -- react too slowly for short-lived,
  high-volatility emerging-tier price action. **Tested and rejected as the
  default (2026-08-20, under the old Solana pipeline)**: walk-forward
  backtesting showed it underperforming `adaptive_ensemble` on every asset
  that had a comparison point, with a large *reversed* train/test gap on top
  -- itself a warning sign of an unstable, noise-driven signal rather than a
  real edge that's merely faster. The shorter windows don't just react
  faster to real trends -- they react faster to one-off pumps too,
  converting the exact trap the original ensemble was built to avoid into an
  inflated backtest number. Kept in `backtest/strategies.py`/`STRATEGIES` as
  a validated-negative baseline for future comparisons, not as a live
  candidate.

- **`macd_crossover`** -- trend-following momentum, using the classic MACD
  construction (fast EMA - slow EMA, crossed against a signal-period EMA of
  that difference) instead of a plain SMA cross. **Backtested 2026-08-26
  across the live-eligible Kraken universe: looked like the best strategy by
  raw average return (+20.6% vs adaptive_ensemble's +11.1%), but this does
  NOT hold up under closer inspection** -- of its profitable non-zero-trade
  results, 18 of 29 (62%) had a **0% closed-trade win rate**, meaning the
  reported gain came from a position still open (unrealized) at the exact
  cutoff of the test window, not a completed, validated round trip.
  `adaptive_ensemble` showed this pattern on 0 of 14 comparable results over
  the same run. MACD's headline number here is largely an artifact of
  "happened to be holding a winner on an arbitrary cutoff date," not a
  demonstrated repeatable edge -- kept in `STRATEGIES` as a real candidate
  worth paper-trading to build a genuine closed-trade track record (pass
  `--strategy macd_crossover` to `paper_trading/run_paper_cycle.py`), but
  NOT promoted to the live default on this evidence alone. Re-run this
  comparison periodically and re-evaluate once it has real paper-trading
  round trips, not just backtest mark-to-market snapshots.
- **`bollinger_mean_reversion`** -- mean-reversion against a volatility-
  adjusted band (SMA +/- N standard deviations) instead of RSI's fixed
  0-100 oscillator, so what counts as "oversold" scales with the asset's
  own recent volatility. **Backtested 2026-08-26: underperformed
  `adaptive_ensemble` on the live-eligible universe** (+5.0% avg return,
  47.5% win rate, vs. `adaptive_ensemble`'s +11.1%/41.0%) -- not a validated
  live candidate on this evidence, kept as a documented negative result
  rather than re-derived from scratch later.

None of the base strategies is inherently "the" strategy -- they suit
different market regimes, which is exactly the problem `adaptive_ensemble`
and `trade-cycle` step 5's conservative-combination rule are trying to
manage rather than ignore.

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
- Total return alone is misleading -- weigh it against max drawdown (can the
  account's circuit breaker survive that path?) and win rate/trade count
  (very few trades means the result isn't statistically meaningful; re-test
  over a longer window).
- Backtests use daily closes from Kraken's own OHLC endpoint, not intraday
  data -- treat results as directional evidence of an edge, not a precise
  forecast of live P&L. Live fills will differ.
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

A flat fraction of the account sized the same into a calm blue-chip and a
wild emerging-tier pair is implicitly taking on far more risk in the
emerging-tier pair per dollar deployed. `config/risk.yaml`'s
`target_daily_volatility_pct` + `volatility_size_min_mult`/`max_mult` scale
the base position size down for higher-volatility assets and up (within a
cap) for lower-volatility ones, so a given trade's *actual risk
contribution* stays closer to constant across very different assets. See
`backtest/strategies.py`'s `realized_vol()` for the reference calculation
the agent should replicate live.

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
the overall tape is deteriorating, even if a specific pair's own signal says
buy. Existing positions still get managed by their own stop-loss/take-profit
regardless of regime state.

### Concentration guardrails

`max_emerging_tier_positions`, `max_same_ecosystem_positions`, and
`max_non_stable_exposure_fraction` in `config/risk.yaml` exist because
several nominally-different pairs can be the same bet in disguise (e.g.
several altcoins that all just move with broad-market beta, or several
emerging-tier pairs that all move on the same sentiment). Diversification
across tickers isn't real diversification if they're all correlated to the
same underlying factor.

## Exit discipline

`stop_loss_pct`, `take_profit_pct`, and `trailing_stop_pct` in
`config/risk.yaml` apply per-position regardless of what the entry strategy
was -- a strategy's job is to decide entries; risk management (this file's
rules, enforced by `trade-cycle`) decides exits independent of whether the
original signal has "changed its mind" yet. This protects against a strategy
that's slow to signal an exit in a fast-moving loss.
