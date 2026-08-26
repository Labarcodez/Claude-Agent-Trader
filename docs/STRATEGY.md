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
  dip" through a real downtrend). **The `paper_trading/run_paper_cycle.py`
  default since 2026-08-26** (see "Trade frequency, not just trade quality"
  below for why) -- still not the *live* default (that stays
  `adaptive_ensemble` in `config/risk.yaml`/`trade-cycle` until this builds
  its own real paper track record, per CLAUDE.md rule 5).
- **`volatility_breakout`** -- buys new highs with confirming volatility,
  sells new lows. Momentum-continuation; sensitive to lookback tuning and
  false breakouts. **Looked strong by raw average return in an early
  2026-08-26 pass, but that was almost entirely buy-and-hold drift**: of 37
  live-eligible-universe backtests, only 3 (8.1%) contained an actual round
  trip, and those 3 had a 0% win rate -- see "Trade frequency, not just
  trade quality" below. Not a live or paper candidate on this evidence.
- **`adaptive_ensemble`** -- regime-aware weighted vote across the three
  above: mostly trend-following signals when `regime()` detects a real trend
  (wide fast/slow SMA gap relative to volatility), mostly RSI mean-reversion
  when it detects chop. The live default in `config/risk.yaml`/`trade-cycle`
  -- not because it's guaranteed better, but because picking one fixed
  strategy means betting the whole account on the market staying in the
  regime that strategy likes. **Real weakness found 2026-08-26, after 170
  live paper cycles produced zero trades beyond the two opened in cycle 1**:
  it only generates an actual (non-drift) trade on 38.9% of the
  live-eligible universe over a full 180-day backtest -- the lowest firing
  rate of any strategy with a clean, non-unrealized-inflated track record.
  When it DOES trade, its record is genuinely the best of any strategy
  tested (+29.13% avg, 100% win rate, 0/14 unrealized-inflated) -- this is a
  frequency problem, not a quality problem. See "Trade frequency, not just
  trade quality" below.
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

- **`stochastic_oscillator`** -- the "slow stochastic" %D line (where the
  latest close sits within its trailing range, smoothed) crossing
  oversold/overbought thresholds. Distinct from RSI: measures *position
  within the recent range*, not the size/speed of recent gains vs. losses --
  the two can disagree. **Backtested 2026-08-26 on hourly bars: fires on
  100% of assets but with a clean, real -2.3% average return** (0/41
  unrealized-inflated) -- genuinely tested and unprofitable at this
  granularity, not just untested. Worse at 15/5-minute (see "Day trading"
  below).
- **`donchian_channel_breakout`** -- classic "turtle trading" breakout: buy
  on any new N-bar high, sell on any new N-bar low, no confirmation buffer
  beyond the extreme itself. Distinct from `volatility_breakout`, which
  requires clearing the extreme by a volatility-scaled margin first (fewer,
  better-confirmed trades). **Backtested 2026-08-26: one of the strongest
  real performers on hourly bars** (+21.0% avg, 97.5% fire rate, 0/39
  unrealized-inflated) -- see "Day trading" below.
- **`ema_ribbon`** -- three-EMA (fast/mid/slow) alignment; fires only on the
  bar where all three newly align bullishly or bearishly, not on every bar
  the alignment holds. Smoother/more selective than `sma_crossover`'s single
  fast/slow cross. **Backtested 2026-08-26: the single best real performer
  found on hourly bars** (+24.2% avg, 82.1% fire rate, 0/32
  unrealized-inflated) -- see "Day trading" below.

None of the base strategies is inherently "the" strategy -- they suit
different market regimes, which is exactly the problem `adaptive_ensemble`
and `trade-cycle` step 5's conservative-combination rule are trying to
manage rather than ignore.

### Trade frequency, not just trade quality

All of these strategies read *daily*-scale closes (SMA10/30, RSI14) --
running `paper_trading/run_paper_cycle.py` every few minutes instead of
every few hours doesn't create more trading opportunities, it just
recomputes the same signal against a bar that hasn't changed yet. 170 live
paper cycles over 12 hours producing zero trades beyond the two opened in
cycle 1 wasn't a cadence problem; it was `adaptive_ensemble` almost never
crossing its own fire threshold, confirmed by then actually measuring how
often each strategy generates a real trade at all, not just what its
average return looks like when it does.

**A strategy's raw average return (or even win rate) computed across every
backtested asset is misleading if most of those backtests never actually
traded** -- a strategy that took zero positions on an asset that happened
to drift upward over the test window gets credited with that drift as if
it were a decision, when `test_round_trips == 0` means nothing was ever
bought or sold. `volatility_breakout` is the clearest example: it looked
competitive on raw average return, but only 3 of 37 live-eligible-universe
backtests (8.1%) contained an actual round trip, and every one of those 3
lost money (0% win rate) -- the "good" number was almost entirely
buy-and-hold drift on assets it never traded.

The fix: filter to `test_round_trips > 0` first, THEN compare average
return/win rate, AND check what fraction of even those real-trade results
still carry a 0%-closed-win-rate-but-positive-return pattern (the
mark-to-market-only artifact documented under `macd_crossover` above) --
three numbers together (fire rate, real-trade return, and what fraction of
that is genuinely realized), not any one of them alone. Re-run this same
three-part check whenever picking a strategy from a backtest, not just the
first time:

```
# fire rate + real-trade return, filtered to test_round_trips > 0
# then, for the same strategy, check what fraction of those had a
# 0%-win-rate-but-positive-return row (== unrealized-only, see
# macd_crossover's writeup above for the exact snippet)
```

A strategy with a low fire rate isn't necessarily bad (`adaptive_ensemble`
has the best per-trade record of anything tested here) -- but it does mean
"run it and wait" can look identical to "it's broken" for a long time, and
a small/short paper-trading window may simply not contain one of its rarer
trade opportunities yet.

### Day trading -- what the evidence actually supports

`kraken.client.ohlc()` (and `backtest/fetch_history.py`,
`backtest/backtest_all.py`, `paper_trading/run_paper_cycle.py`) support
`interval_minutes` (5/15/30/60/240, default 1440/daily) so any strategy can
be backtested and paper-traded at day-trading granularity, not just daily.
Kraken's OHLC endpoint caps at ~720 bars regardless of interval, which
bounds how much history a short interval can even provide (5-minute bars:
~2.5 days max; 15-minute: ~7.5 days; hourly: ~30 days) -- a real ceiling,
not a "not enough history" edge case.

**Actually tested (2026-08-26), applying the same trade-frequency-adjusted
methodology above at each granularity:**

| Interval | Verdict | Real evidence |
|---|---|---|
| **60 min (hourly)** | **Genuinely promising** | `ema_ribbon` (+24.2%, 82% fire rate, 0/32 unrealized-inflated), `donchian_channel_breakout` (+21.0%, 97.5% fire rate, 0/39 inflated), `volatility_breakout` (+23.8%, 92.5% fire rate -- a complete reversal from its poor *daily*-bar showing), `sma_crossover` (+18.4%, 100% fire rate) all show clean, real, high-frequency edge. |
| 15 min | **Not profitable** | EVERY strategy tested showed a *negative* average return once real trade frequency was counted, including ones with a nominal 100% win rate (`macd_crossover`: 100% win rate, **-12.9%** average return) -- fee/slippage drag from trading that often overwhelms each individual trade's edge. |
| 5 min | **Not profitable, worse** | Same pattern, more pronounced. The one nominally-positive result fired on only 25.6% of assets over a 2-day window -- too small a sample to trust. |

This is the fee-drag problem `docs/STRATEGY.md`'s "core problem with a
small account" already names, now measured directly: a strategy's edge per
trade has to outrun its trading frequency's fee cost, and for these
strategies on Kraken's real fee schedule, hourly clears that bar while
15-minute and 5-minute don't.

**A real methodology caveat found while re-verifying this (2026-08-26):**
re-running `backtest_all.py --interval-minutes 60` minutes apart, on the
same code, produced wildly different hourly results across attempts made
during that day's development session -- one run showed the strong numbers
in the table above, two others (made ~2-4 minutes later) showed every
strategy *negative* on the same assets. Live OHLC data can't plausibly
shift that much in minutes, so this was almost certainly a code-in-flux
artifact of active same-session development, not evidence the underlying
edge is this unstable -- but it could not be fully root-caused after the
fact. A clean re-run against the current, fully-tested codebase reproduced
the strong table above (see `backtest_all_20260826T192717Z.json`), which is
why hourly mode is still recommended -- but given this, treat any single
`backtest_all.py` run with real skepticism and re-run before trusting a
result, even more than "Judging a backtest" below already recommends.

**Frequency vs. quality, from that clean re-verification run** (40 assets,
same honest methodology -- real trades only, unrealized-inflated excluded):

| Strategy | Total round-trips (40 assets) | Avg return | Win rate |
|---|---|---|---|
| `sma_crossover` | 138 | +18.9% | 54.8% |
| `donchian_channel_breakout` | 97 | +19.8% | 60.8% |
| `volatility_breakout` | 55 | +22.4% | 80.6% |
| `ema_ribbon` | 52 | +22.4% | 79.2% |

All four are real, evidence-backed hourly candidates -- the honest
trade-off is frequency against win rate, not "some are fake." Live paper
default as of 2026-08-26 is **`donchian_channel_breakout`**, chosen for
meaningfully more trade frequency than `ema_ribbon` (+87%) while keeping a
respectable win rate; `sma_crossover` is the more-aggressive-still option if
even more frequency is wanted at a lower win rate, and `ema_ribbon`/
`volatility_breakout` are the higher-win-rate, lower-frequency alternative
if that's ever preferred instead. **Use `--interval-minutes 60` with
`--strategy donchian_channel_breakout`** for real day trading
(`paper_trading/run_paper_cycle.py --interval-minutes 60 --strategy
donchian_channel_breakout`) -- the infrastructure supports 15/5-minute bars
for future re-testing (a different asset universe, a different fee tier, or
a strategy actually designed for that noise floor could change this), but
today's evidence says don't trade on them.

**This paper-tracks separately from the daily-mode default** -- switching
`--interval-minutes`/`--strategy` mid-run mixes decisions made under
different signal engines into the same `state/paper_portfolio.json`
history. Run `--reset` when starting a day-trading paper track record you
want to judge cleanly against the daily-mode one.

**Also wired in alongside this**: every new position now has to clear
`kraken.fees.edge_clears_costs()` before it's opened --
`config/risk.yaml`'s `min_edge_to_cost_multiple` (default 2.0x) -- checked
against the account's REAL current Kraken fee tier
(`kraken.client.trade_volume()`) when an API key is configured, not a flat
assumption. This matters more the more often the account trades, which is
exactly what day trading means -- a signal that can't clear its own real
costs by a healthy margin is rejected before it ever becomes a trade,
logged in `not_traded` the same as any other rejection reason.

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

### 2026-08-26 sizing increase

At the user's explicit request to size more aggressively:
`max_position_fraction` 0.30->0.40, `max_position_usd_pct` 0.40->0.50 (both
`config/risk.yaml` and `paper_trading/run_paper_cycle.py`'s CLI defaults,
kept in sync), plus paper-trading-only `scout_position_fraction` 0.20->0.30,
`scout_min_trade_usd` 2.5->5.0, and `max_memecoin_exposure_fraction`
0.20->0.40 (the cap that actually governs how many scout/emerging positions
can coexist -- raising `max_scout_positions` alone would have been a no-op
without this, since the exposure cap binds first at the new $5 floor).
Verified live: a reset cycle at the new $100 starting capital opened 6
positions across established/emerging/scout tiers (vs. the old $2.50-floor-
everywhere behavior), sized from $7.20 (scout, vol-scaled up) to $42
(ETHUSD, established tier) before the exposure cap correctly blocked a 7th.

Found in the process, not yet resolved (a separate decision, not bundled
into this change): `paper_trading/run_paper_cycle.py`'s
`--max-concurrent-positions` CLI default is 15, not risk.yaml's documented
`max_concurrent_positions: 3` -- unlike `max_emerging_tier_positions`
(which does match), this one drifted from the "keep in sync by hand"
convention at some point before this session. Paper mode has been running
more concurrent positions than live trading is configured to allow; this
doesn't carry forward automatically since risk.yaml's value (not whatever
paper defaults to) is what would govern a real account.

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

## Fee-aware execution

`kraken/fees.py` computes what a trade actually costs at the account's real
current fee tier (`kraken.client.trade_volume()`), not a flat assumption --
Kraken's maker/taker rates depend on live 30-day volume, so the same
position size can cost meaningfully different amounts as the account trades
more. `edge_clears_costs()` checks whether a position's take-profit target,
if hit, would still net comfortably more than both legs' fees (plus entry
spread for a market/taker order) -- a trade that only clears its own costs
by a hair isn't worth the tail risk of a worse-than-modeled fill eating the
rest. `kraken/propose_order.py` surfaces a real-fee-tier round-trip cost
*estimate* on every quote automatically; `edge_clears_costs()` itself takes
a take-profit target as input, so it's meant to be called from
`trade-cycle` step 7 (which has that position-level context) rather than
the single-order CLI. `kraken/precision.py`'s `clamp_to_pair_minimums()`
separately checks a proposed size against Kraken's own per-pair
`ordermin`/`costmin` (which can exceed `config/risk.yaml`'s project-level
`min_trade_usd` for a thin or expensive pair) -- `kraken/propose_order.py`
runs this automatically on every quote too, so an order that Kraken would
reject on size alone says so before anyone runs `--execute`.

## Exit discipline

`stop_loss_pct`, `take_profit_pct`, and `trailing_stop_pct` in
`config/risk.yaml` apply per-position regardless of what the entry strategy
was -- a strategy's job is to decide entries; risk management (this file's
rules, enforced by `trade-cycle`) decides exits independent of whether the
original signal has "changed its mind" yet. This protects against a strategy
that's slow to signal an exit in a fast-moving loss.
