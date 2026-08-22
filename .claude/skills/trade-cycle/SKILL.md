---
name: trade-cycle
description: Run one autonomous trading cycle for the Claude-Agent-Trader Solana wallet -- check safety gates, run live token discovery, pull market research, generate signals, size and execute at most one or two trades within risk limits, and log everything. Use when the user asks to run a trading cycle, check the portfolio and trade, or when invoked on a schedule (/loop, a Routine) for autonomous operation. Requires the Phantom MCP server to be connected and authenticated in this session.
---

# Trade cycle

One invocation of this skill = one full discover-research-decide-execute-log
cycle for the agent's Solana wallet. There is no hand-maintained token list
-- eligible tokens come from live discovery, run fresh each cycle (see step
3). It is meant to be run repeatedly (manually, via `/loop`, or a scheduled
Routine) -- see `docs/RUNBOOK.md`. Follow these steps **in order**, and stop
at the first gate that fails.

## 0. Confirm environment

Check that a Phantom MCP tool (e.g. `get_wallet_addresses`) is actually
available in this session. If Phantom's MCP server isn't connected, stop and
tell the user -- Phantom's auth is a local browser flow, so this only works in
a session running on a machine that has completed that auth (see
`docs/PHANTOM_MCP_SETUP.md`). Do not attempt to simulate wallet state.

## 0.5. Acquire the cycle lock

Before touching `state/starting_capital.json`, `state/circuit_breaker.json`,
or `journal/trades.jsonl`, acquire the same cross-process lock
`paper_trading/run_paper_cycle.py` uses (`scripts/cycle_lock.py`'s
`CycleLock`) -- this is real money now, and this skill is even more exposed
to the concurrent-process race that lock was built for than paper trading
was: there's no single Python process managing the writes atomically here,
just an LLM session reading and writing files across several separate tool
calls, so a second concurrent trade-cycle run (this session's cron loop and
a local terminal session both running it, a real setup this project has
seen) has a wide window to race on the same state. Mining
`journal/paper_trades.jsonl` found the exact failure signature this
prevents: two near-simultaneous decisions on the same symbol, seconds
apart, because two processes each read the same pre-trade state before
either wrote back.

Run this once, at the very start of the cycle, and wait for it to return
(it blocks until any concurrent cycle releases, or until a stale lock times
out on its own):
```
python3 -c "import sys; sys.path.insert(0, '.'); from pathlib import Path; from scripts.cycle_lock import CycleLock; CycleLock(Path('state/trade_cycle.lock')).acquire()"
```
Then run this exact command as the LAST thing you do this cycle, no matter
how the cycle ends (a normal hold/trade, an early stop at a failed gate, a
circuit breaker trip, or an unexpected error) -- an unreleased lock blocks
every future cycle, including the next scheduled one, until its 280s
timeout breaks it:
```
python3 -c "import sys; sys.path.insert(0, '.'); from pathlib import Path; from scripts.cycle_lock import CycleLock; CycleLock(Path('state/trade_cycle.lock')).release()"
```

## 1. Safety gate (do this before anything else touches the wallet)

1. Read `config/risk.yaml`. If the file is missing, or `enabled: false`,
   stop immediately -- report "trading disabled" and do not proceed.
2. Read `state/circuit_breaker.json`. If `tripped: true`, stop immediately --
   report the tripped reason and do not proceed. (A human must investigate,
   fix the underlying cause, and reset the breaker before trading resumes --
   see `docs/RUNBOOK.md` "Recovering from a circuit breaker trip".)
3. Read `config/core_assets.yaml` (SOL, USDC) and `config/discovery.yaml`
   (sources, thresholds, `pinned_candidates`, `denylist`).

## 2. Get current wallet state

Use the Phantom MCP tools to get:
- The agent wallet's address(es) (`get_wallet_addresses`).
- Current SOL/USDC balances and every currently-held token position
  (whatever's actually in the wallet, whether or not it's still a discovery
  candidate this cycle -- it needs to be trackable for exit either way).
- Mark each balance to USD (use CoinGecko or the Phantom quote tools).

Compute `portfolio_value_usd` = sum of all mark-to-market balances.

**Establish starting capital (there is no fixed/required amount -- whatever
is actually in the wallet is what gets traded):**
- If `state/starting_capital.json` doesn't exist yet, this is the first live
  cycle. Set `starting_capital_usd` = the `portfolio_value_usd` you just
  computed, and write it to `state/starting_capital.json` (e.g.
  `{"starting_capital_usd": <value>, "captured_at": "<ISO timestamp>"}`) so
  later P&L swings never silently redefine the baseline. Otherwise, read the
  persisted value from that file -- do not recompute it from the live
  balance on every cycle.
- Derive this cycle's dollar-denominated risk caps from that persisted value
  and `config/risk.yaml`'s `_pct` siblings (all three are `null` in the YAML
  by design -- they're computed here, not hardcoded):
  - `max_position_usd = starting_capital_usd * max_position_usd_pct`
  - `max_daily_volume_usd = starting_capital_usd * max_daily_volume_usd_pct`
  - `circuit_breaker_floor_usd = starting_capital_usd * circuit_breaker_floor_pct`
  Use these computed values everywhere below (step 7, the circuit breaker
  check) instead of the `null` placeholders in the YAML.

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
need to hit the free APIs again for a cycle running shortly after the last
one.)

This gives every discovered token's safety data and an `eligible: true/false`
verdict computed from live, on-chain-backed checks -- see
`docs/STRATEGY.md` "Autonomous discovery" for what each check means and why.
From the output:

- Every `eligible: true` token, plus `config/discovery.yaml`'s
  `pinned_candidates` (still must currently show `eligible: true` in the
  data to actually be traded -- pinning means "always consider," not "skip
  the safety check"), forms this cycle's tradeable universe alongside SOL/USDC.
- Remove anything on `config/discovery.yaml`'s `denylist`, no exceptions.
- Rejected candidates get a one-line `"proposal_rejected"` journal note
  (symbol, mint, top reason) so there's a record of what was considered and
  why it didn't qualify -- useful for tuning thresholds later, not just for
  audit purposes.
- Any token currently held in the wallet (step 2) that ISN'T in this cycle's
  eligible set is still manageable for exit (stop-loss/take-profit/manual
  sell) -- discovery gates new entries, never blocks getting out of an
  existing position.

## 4. Market regime filter (gates new entries only)

If `regime_filter_enabled` in `config/risk.yaml`, fetch
`regime_reference_coin`'s (default: bitcoin) price history and compute
whether it's currently above its own `regime_sma_window_days`-day SMA (same
logic as `backtest/strategies.py`'s `regime()` helper, applied to the
reference coin). If it's below -- "risk off" -- **do not open any new
position this cycle**, discovered or pinned, emerging-tier or blue-chip.
Exits, stop-losses, and take-profits on existing positions are never gated
by this; protecting capital always outranks the regime filter.

## 5. Research

For every eligible candidate from step 3 (plus any open position needing
price data for exit management):

- Pull price and recent history via CoinGecko or the discovery data already
  in hand. Note: very new/small ("emerging" tier) tokens often aren't on
  CoinGecko at all -- fall back to Jupiter/DexScreener price data for these
  (already present in the discovery JSON), and treat the lack of a
  backtestable price history honestly (see step 6).
- Re-confirm `min_liquidity_usd` right now, not just from the discovery run
  a few minutes ago -- liquidity can move fast, especially for emerging-tier
  tokens.

## 6. Generate signals

For each eligible candidate with enough price history, compute a signal
using `backtest/strategies.py`'s `adaptive_ensemble` (preferred once it has
a non-negative, low-overfit-risk out-of-sample result on file -- see
`.claude/skills/backtest-strategy`) or another strategy documented in
`docs/STRATEGY.md` as validated.

**A specific token with no backtestable history** (common for freshly
discovered emerging-tier tokens) can still be traded -- discovery's own
safety checks already gate out the worst cases -- but only at the smallest
allowed size (`min_trade_usd`, or the tier's minimum), with the tightest
stop-loss, since there's no historical confirmation the strategy has edge on
*this specific token*, only that the strategy has edge on similar assets
generally. Do not size a no-history token as if it had a validated edge.

A single strategy's stop-loss/take-profit trigger is always enough to exit
-- protecting capital doesn't need confirmation.

## 7. Position sizing & risk checks

Before proposing any trade, check ALL of:

- [ ] Token is SOL/USDC, or was `eligible: true` in this cycle's discovery
      run and is not on the denylist (or this is an exit of an existing position)
- [ ] `max_concurrent_positions` not exceeded (for a new entry)
- [ ] `max_emerging_tier_positions` not exceeded, for a new emerging-tier entry
- [ ] `max_same_ecosystem_positions` not exceeded
- [ ] `max_non_stable_exposure_fraction` not exceeded after this trade
- [ ] Regime filter allows new entries (step 4)
- [ ] Base position size = `min(max_position_usd, portfolio_value_usd * max_position_fraction) * tier_multiplier`
      (tier multipliers from `config/discovery.yaml`'s `tiers` block)
- [ ] Volatility-scaled size = `base_size * clamp(target_daily_volatility_pct / realized_20d_volatility_pct, volatility_size_min_mult, volatility_size_max_mult)`
      (skip this scaling -- use the tier's minimum size instead -- for a token with no price history per step 6),
      and the result is `>= min_trade_usd` (else skip -- too small to be worth fees)
- [ ] Adding this position keeps `sum(position_size_usd * stop_loss_pct) / portfolio_value_usd <= max_portfolio_heat_pct`
      (portfolio heat -- compute across ALL open positions including this candidate)
- [ ] Today's cumulative trade count < `max_daily_trade_count`
- [ ] Today's cumulative trade volume + this trade < `max_daily_volume_usd`
- [ ] At least `min_hours_between_trades_same_token` since the last trade in this token
- [ ] Quoted slippage <= `max_slippage_bps` and price impact <= `max_price_impact_bps`
      (get a quote first, e.g. via `buy_token` in quote-only/simulate mode, before executing)

If any check fails, do not trade that signal -- log why and move on.

## 8. Execute

For each trade that passes every check (cap this at a small number per
cycle -- 1-2 trades is normal; if many signals fire at once, that's a reason
for suspicion, not a reason to fire them all):

1. Get a fresh quote (`buy_token` or equivalent) and re-verify slippage/impact.
2. Execute the swap via the Phantom MCP tool.
3. Record the transaction signature and fill details.
4. Re-fetch balances to confirm the trade landed as expected.

If a call errors or the fill looks wrong (e.g. price far off the quote),
stop executing further trades this cycle and log the anomaly clearly --
do not retry blindly.

## 9. Log

Append one entry to `journal/trades.jsonl` per `journal/README.md`'s format,
covering the discovery result summary (how many candidates found/eligible/
rejected), every eligible token considered this cycle for trading (not just
ones traded), the risk-check results (including portfolio heat, regime
state, and tier), and the action taken (including explicit `"hold"`
entries). Update `state/circuit_breaker.json`'s `peak_portfolio_value_usd`
if applicable.

## 10. Report

Give the user a short summary: portfolio value before/after, discovery stats
(candidates found / eligible / rejected, and why the rejections failed if
notable), what was traded (if anything) and why, current open positions,
current regime state and portfolio heat, and whether anything needs their
attention (a circuit breaker trip, a repeated execution anomaly, a
discovery-threshold that seems to be rejecting everything or passing too
much).
