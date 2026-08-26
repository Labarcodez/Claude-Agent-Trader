---
name: trade-cycle
description: Run one autonomous trading cycle for the Claude-Agent-Trader Kraken account -- check safety gates, run live pair discovery, pull market research, generate signals, size and execute at most one or two trades within risk limits, and log everything. Use when the user asks to run a trading cycle, check the portfolio and trade, or when invoked on a schedule (/loop, a Routine) for autonomous operation. Requires the Kraken MCP server (kraken-cli) to be connected, authenticated, and able to reach api.kraken.com in this session.
---

# Trade cycle

One invocation of this skill = one full discover-research-decide-execute-log
cycle for the agent's Kraken account. There is no hand-maintained pair list
-- eligible pairs come from live discovery, run fresh each cycle (see step
3). It is meant to be run repeatedly (manually, via `/loop`, or a scheduled
Routine) -- see `docs/RUNBOOK.md`. Follow these steps **in order**, and stop
at the first gate that fails.

## 0. Confirm environment

Check that a Kraken MCP tool (e.g. `balance`) is actually available and can
reach Kraken in this session. Two independent things can block this, and
they fail differently:

- **The MCP server isn't connected at all** -- no Kraken tools show up.
  Confirm `kraken` is on `PATH` and `KRAKEN_API_KEY`/`KRAKEN_API_SECRET` are
  set (see `docs/KRAKEN_CLI_SETUP.md`). Unlike the project's original
  Phantom/Solana version, Kraken's auth is a plain API key + secret, not a
  browser sign-in -- there is no fundamental reason this can't run in a
  cloud/remote session, only whether the credentials and network access are
  actually configured there.
- **The tool is available but every call times out or gets refused** -- this
  session's network egress policy may not allow outbound calls to
  `api.kraken.com`. This is a per-environment setting (see the Claude Code
  on the web docs on network policy), not a Kraken limitation -- check with
  the user whether this session's environment allows it, and don't retry
  indefinitely against a policy block. `research/discover_candidates.py` and
  `paper_trading/run_paper_cycle.py` hit the same public endpoint and will
  fail the same way if this is blocked; if they don't work either, say so
  plainly rather than fabricating results (this is this project's
  equivalent of the old "no local browser" caveat -- same bottom line,
  different mechanism).

If neither works, stop and tell the user -- do not attempt to simulate
account state.

## 1. Safety gate (do this before anything else touches the account)

1. Read `config/risk.yaml`. If the file is missing, or `enabled: false`,
   stop immediately -- report "trading disabled" and do not proceed.
2. Read `state/circuit_breaker.json`. If `tripped: true`, stop immediately --
   report the tripped reason and do not proceed. (A human must investigate,
   fix the underlying cause, and reset the breaker before trading resumes --
   see `docs/RUNBOOK.md` "Recovering from a circuit breaker trip".)
3. Read `config/core_assets.yaml` (USD, USDT) and `config/discovery.yaml`
   (sources, thresholds, `pinned_candidates`, `denylist`).
4. Confirm `config/risk.yaml`'s `asset_classes` -- only place orders in an
   asset class marked `enabled: true`. `kraken_spot` is the only one enabled
   by default; `kraken_margin` and `kraken_futures_perps` must stay off
   unless a human has explicitly turned them on (they carry leverage and
   liquidation risk this project's sizing/circuit-breaker math isn't built
   for). Never place a margin or leveraged-futures order just because the
   Kraken MCP server exposes the tool for it.

## 2. Get current account state

Use the Kraken MCP tools (or CLI directly) to get:
- All balances (`balance`), including USD/USDT cash and every currently-held
  position (whatever's actually in the account, whether or not it's still a
  discovery candidate this cycle -- it needs to be trackable for exit
  either way).
- Mark each non-cash balance to USD (use `ticker` for the relevant pair, or
  the CoinGecko MCP tool if available).

Compute `portfolio_value_usd` = sum of all mark-to-market balances,
**including any amount currently allocated to Kraken Earn** (step 9) -- it's
still the account's capital, just working instead of idle.

**Establish starting capital (there is no fixed/required amount -- whatever
is actually in the account is what gets traded):**
- If `state/starting_capital.json` doesn't exist yet, this is the first live
  cycle. Set `starting_capital_usd` = the `portfolio_value_usd` you just
  computed, and write it to `state/starting_capital.json` (e.g.
  `{"starting_capital_usd": <value>, "captured_at": "<ISO timestamp>"}`) so
  later P&L swings never silently redefine the baseline. Otherwise, read the
  persisted value from that file -- do not recompute it from the live
  balance on every cycle.
- Derive this cycle's dollar-denominated risk caps from that persisted value
  and `config/risk.yaml`'s `_pct` siblings (all `null` in the YAML by
  design -- they're computed here, not hardcoded):
  - `max_position_usd = starting_capital_usd * max_position_usd_pct`
  - `max_daily_volume_usd = starting_capital_usd * max_daily_volume_usd_pct`
  - `circuit_breaker_floor_usd = starting_capital_usd * circuit_breaker_floor_pct`
  - `earn_allocate_idle_cash_above_usd = starting_capital_usd * earn_allocate_idle_cash_above_usd_pct`
  Use these computed values everywhere below instead of the `null`
  placeholders in the YAML.

**Check the circuit breaker condition right now, before trading:**
- If `portfolio_value_usd <= circuit_breaker_floor_usd` (computed above), OR
- If `portfolio_value_usd` has dropped more than `circuit_breaker_daily_loss_pct`
  from the highest `portfolio_value_usd` recorded in `journal/trades.jsonl`
  within the last 24h (use `state/circuit_breaker.json`'s
  `peak_portfolio_value_usd` if the journal is empty/unavailable -- if
  *that* is also `null` because this is the first cycle ever, there is no
  peak yet: initialize it to the current `portfolio_value_usd` and skip this
  drawdown-from-peak leg for this cycle only),

then: set `state/circuit_breaker.json` `tripped: true` with a reason and
`portfolio_value_usd_at_trip`, append a journal entry explaining the trip,
report it clearly to the user, and **stop -- do not place any trade this
cycle.**

Otherwise, update `peak_portfolio_value_usd` in `state/circuit_breaker.json`
if the current value is a new high (or it was just initialized above).

## 3. Discover candidates

Run:
```
python3 research/discover_candidates.py
```
(Skip this if a `research/results/discovery_*.json` file exists and is
younger than `config/risk.yaml`'s `max_discovery_result_age_hours` -- no
need to hit Kraken's public endpoints again for a cycle running shortly
after the last one.)

This gives every evaluated pair's live liquidity/spread data and an
`eligible: true/false` verdict -- see `docs/STRATEGY.md` "Autonomous
discovery" for what each check means and why. From the output:

- Every `eligible: true` pair, plus `config/discovery.yaml`'s
  `pinned_candidates` (still must currently show `eligible: true` in the
  data to actually be traded -- pinning means "always consider," not "skip
  the safety check"), forms this cycle's tradeable universe alongside
  USD/USDT.
- Remove anything on `config/discovery.yaml`'s `denylist`, no exceptions.
- Rejected candidates get a one-line `"proposal_rejected"` journal note
  (altname, top reason) so there's a record of what was considered and why
  it didn't qualify -- useful for tuning thresholds later, not just for
  audit purposes.
- Any pair currently held in the account (step 2) that ISN'T in this cycle's
  eligible set is still manageable for exit (stop-loss/take-profit/manual
  sell) -- discovery gates new entries, never blocks getting out of an
  existing position.
- **Don't assume the discovery script's `altname` is exactly what Kraken
  CLI/MCP expects as the order symbol.** Cross-check with `kraken pairs
  --pair <altname>` (or the MCP equivalent) before placing an order -- see
  `research/discover_candidates.py`'s own closing note.

## 4. Market regime filter (gates new entries only)

If `regime_filter_enabled` in `config/risk.yaml`, fetch
`regime_reference_pair`'s (default: `XBTUSD`) price history and compute
whether it's currently above its own `regime_sma_window_days`-day SMA (same
logic as `backtest/strategies.py`'s `regime()` helper, applied to the
reference pair). If it's below -- "risk off" -- **do not open any new
position this cycle**, discovered or pinned, emerging-tier or blue-chip.
Exits, stop-losses, and take-profits on existing positions are never gated
by this; protecting capital always outranks the regime filter.

## 5. Research

For every eligible candidate from step 3 (plus any open position needing
price data for exit management):

- Pull price and recent history via Kraken's own OHLC/Ticker (or the
  CoinGecko MCP tool for a longer-history cross-check on majors).
- Re-confirm `min_24h_quote_volume_usd` and spread right now, not just from
  the discovery run a few minutes ago -- both can move fast, especially for
  emerging-tier pairs.

## 6. Generate signals

For each eligible candidate with enough price history, compute a signal
using `backtest/strategies.py`'s `adaptive_ensemble` (preferred once it has
a non-negative, low-overfit-risk out-of-sample result on file -- see
`.claude/skills/backtest-strategy`) or another strategy documented in
`docs/STRATEGY.md` as validated.

**A specific pair with no backtestable history** can still be traded --
discovery's own liquidity/spread checks already gate out the worst cases --
but only at the smallest allowed size (`min_trade_usd`, or the tier's
minimum, whichever is larger than the pair's own Kraken `ordermin`/`costmin`
from `config/discovery.yaml`'s discovery output), with the tightest
stop-loss, since there's no historical confirmation the strategy has edge on
*this specific pair*, only that the strategy has edge on similar assets
generally. Do not size a no-history pair as if it had a validated edge.

A single strategy's stop-loss/take-profit trigger is always enough to exit
-- protecting capital doesn't need confirmation.

## 7. Position sizing & risk checks

Before proposing any trade, check ALL of:

- [ ] Pair is USD/USDT-settled and was `eligible: true` in this cycle's
      discovery run and is not on the denylist (or this is an exit of an
      existing position)
- [ ] `max_concurrent_positions` not exceeded (for a new entry)
- [ ] `max_emerging_tier_positions` not exceeded, for a new emerging-tier entry
- [ ] `max_same_ecosystem_positions` not exceeded
- [ ] `max_non_stable_exposure_fraction` not exceeded after this trade
- [ ] Regime filter allows new entries (step 4)
- [ ] Base position size = `min(max_position_usd, portfolio_value_usd * max_position_fraction) * tier_multiplier`
      (tier multipliers from `config/discovery.yaml`'s `tiers` block)
- [ ] Volatility-scaled size = `base_size * clamp(target_daily_volatility_pct / realized_20d_volatility_pct, volatility_size_min_mult, volatility_size_max_mult)`
      (skip this scaling -- use the tier's minimum size instead -- for a pair with no price history per step 6),
      and the result is `>= min_trade_usd` AND `>= the pair's own Kraken costmin` (else skip -- too small to be worth fees, or below what Kraken will even accept)
- [ ] Adding this position keeps `sum(position_size_usd * stop_loss_pct) / portfolio_value_usd <= max_portfolio_heat_pct`
      (portfolio heat -- compute across ALL open positions including this candidate)
- [ ] Today's cumulative trade count < `max_daily_trade_count`
- [ ] Today's cumulative trade volume + this trade < `max_daily_volume_usd`
- [ ] At least `min_hours_between_trades_same_pair` since the last trade in this pair
- [ ] Live spread `<= max_spread_bps`, and (for a market/taker order) enough
      resting order-book depth within `max_slippage_bps` of the mid price to
      absorb this size without excess impact (`kraken orderbook`)

If any check fails, do not trade that signal -- log why and move on.

## 8. Execute

For each trade that passes every check (cap this at a small number per
cycle -- 1-2 trades is normal; if many signals fire at once, that's a reason
for suspicion, not a reason to fire them all):

1. Per `config/risk.yaml`'s `prefer_maker_orders`, place a **limit** order at
   or inside the current best bid/ask when the signal isn't time-critical,
   to capture Kraken's lower maker fee rather than paying the full taker
   rate on every entry -- see `docs/STRATEGY.md` "Fee-aware execution". Use
   a market order only when the signal calls for immediate execution (e.g.
   a stop-loss exit).
2. Place the order via the Kraken MCP trade tool (`order buy`/`order sell`).
   Prefer Kraken's own native `stop-loss`/`take-profit`/`trailing-stop`
   order types for exits where practical, rather than only checking them in
   software each cycle -- a resting order on Kraken survives even if a
   future trade-cycle run is missed or delayed.
3. Record the order/transaction id and fill details.
4. Re-fetch balances to confirm the trade landed as expected.

If a call errors or the fill looks wrong (e.g. price far off the quote),
stop executing further trades this cycle and log the anomaly clearly --
do not retry blindly.

## 9. Idle-capital yield (Kraken Earn) -- optional, spot-only

If `config/risk.yaml`'s `asset_classes.kraken_earn.enabled` is true:

- After sizing/executing this cycle's trades, compute leftover cash not
  needed as trade-ready buffer: `idle_cash = cash_usd - earn_allocate_idle_cash_above_usd`
  (computed in step 2). If `idle_cash > 0`, consider allocating it to a
  Kraken Earn **flexible** product for the held asset (USD/USDT stable-yield
  product, or an already-held position's own flexible staking product where
  offered).
- **Hard rule: `flexible_only: true` in `config/risk.yaml` means literally
  only flexible/instant-unstake Earn products -- never Bonded Earn or
  anything with an on-chain unbonding period.** Check the product's terms
  before allocating; if unstake isn't immediate, don't use it. This is what
  keeps Earn from silently defeating the circuit breaker (see
  `config/risk.yaml`'s comment on this).
- Before any circuit-breaker response, a withdrawal, or resizing a position
  that needs the cash, deallocate first, then act -- don't let allocated
  Earn balance count as "available cash" until it's actually back in the
  spendable balance.
- Log every allocate/deallocate action in the journal the same as a trade.

## 10. Log

Append one entry to `journal/trades.jsonl` per `journal/README.md`'s format,
covering the discovery result summary (how many candidates evaluated/
eligible/rejected), every eligible pair considered this cycle for trading
(not just ones traded), the risk-check results (including portfolio heat,
regime state, and tier), the action taken (including explicit `"hold"`
entries), and any Earn allocate/deallocate action. Update
`state/circuit_breaker.json`'s `peak_portfolio_value_usd` if applicable.

## 11. Report

Give the user a short summary: portfolio value before/after, discovery stats
(candidates evaluated/eligible/rejected, and why the rejections failed if
notable), what was traded (if anything) and why, current open positions,
current regime state and portfolio heat, any Earn allocation change, and
whether anything needs their attention (a circuit breaker trip, a repeated
execution anomaly, a discovery-threshold that seems to be rejecting
everything or passing too much).
