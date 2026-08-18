---
name: trade-cycle
description: Run one autonomous trading cycle for the Claude-Agent-Trader Solana wallet -- check safety gates, pull market research, generate signals, size and execute at most one or two trades within risk limits, and log everything. Use when the user asks to run a trading cycle, check the portfolio and trade, or when invoked on a schedule (/loop, a Routine) for autonomous operation. Requires the Phantom MCP server to be connected and authenticated in this session.
---

# Trade cycle

One invocation of this skill = one full research-decide-execute-log cycle for
the agent's Solana wallet. It is meant to be run repeatedly (manually, via
`/loop`, or a scheduled Routine) -- see `docs/RUNBOOK.md` for how to start
that. Follow these steps **in order**, and stop at the first gate that fails.

## 0. Confirm environment

Check that a Phantom MCP tool (e.g. `get_wallet_addresses`) is actually
available in this session. If Phantom's MCP server isn't connected, stop and
tell the user -- Phantom's auth is a local browser flow, so this only works in
a session running on a machine that has completed that auth (see
`docs/PHANTOM_MCP_SETUP.md`). Do not attempt to simulate wallet state.

## 1. Safety gate (do this before anything else touches the wallet)

1. Read `config/risk.yaml`. If the file is missing, or `enabled: false`,
   stop immediately -- report "trading disabled" and do not proceed.
2. Read `state/circuit_breaker.json`. If `tripped: true`, stop immediately --
   report the tripped reason and do not proceed. (A human must investigate,
   fix the underlying cause, and reset the breaker before trading resumes --
   see `docs/RUNBOOK.md` "Recovering from a circuit breaker trip".)
3. Read `config/watchlist.yaml`. Only entries with `verified: true` are ever
   tradeable (`require_verified_flag` in risk.yaml) -- treat any
   `verified: false` entry (including meme coins) as informational only
   until a human has actually checked the mint address and flipped it.

## 2. Get current wallet state

Use the Phantom MCP tools to get:
- The agent wallet's address(es) (`get_wallet_addresses`).
- Current balances for SOL, USDC, and every watchlist token held.
- Mark each balance to USD (use CoinGecko or the Phantom quote tools).

Compute `portfolio_value_usd` = sum of all mark-to-market balances.

**Check the circuit breaker condition right now, before trading:**
- If `portfolio_value_usd <= circuit_breaker_floor_usd`, OR
- If `portfolio_value_usd` has dropped more than `circuit_breaker_daily_loss_pct`
  from the highest `portfolio_value_usd` recorded in `journal/trades.jsonl`
  within the last 24h (use `state/circuit_breaker.json`'s
  `peak_portfolio_value_usd` if the journal is empty/unavailable),

then: set `state/circuit_breaker.json` `tripped: true` with a reason and
`portfolio_value_usd_at_trip`, append a journal entry explaining the trip,
report it clearly to the user, and **stop -- do not place any trade this
cycle.**

Otherwise, update `peak_portfolio_value_usd` in `state/circuit_breaker.json`
if the current value is a new high.

## 3. Market regime filter (gates new entries only)

If `regime_filter_enabled` in `config/risk.yaml`, fetch
`regime_reference_coin`'s (default: bitcoin) price history and compute
whether it's currently above its own `regime_sma_window_days`-day SMA (same
logic as `backtest/strategies.py`'s `regime()` helper, applied to the
reference coin). If it's below -- "risk off" -- **do not open any new
position this cycle**, meme or otherwise. Exits, stop-losses, and
take-profits on existing positions are never gated by this; protecting
capital always outranks the regime filter.

## 4. Research

For every token on the watchlist (plus any currently open position even if
since removed from the watchlist, so it can still be exited):

- Pull price, 24h change, 24h volume, and liquidity. Prefer a market-data MCP
  tool if one is connected in this session (e.g. CoinGecko); otherwise fall
  back to a direct HTTPS call to `https://api.coingecko.com/api/v3/...`
  (no key required) via whatever fetch tool is available.
- Confirm `min_liquidity_usd` and reasonable 24h volume are still met --
  liquidity can evaporate; re-check every cycle, don't trust the watchlist
  note alone. This matters even more for meme-category tokens (see
  `docs/STRATEGY.md` "Meme coin handling") -- their liquidity can vanish in
  hours, not weeks.
- Use a trending/discovery tool (e.g. CoinGecko `get-trending`) as a
  **candidate-generation** input, including for meme coins -- this is
  explicitly in scope; the agent may propose any token, including memes,
  that looks like it can make money. But discovery never skips the gate
  below: any candidate not already watchlisted and `verified: true` gets
  logged as a `"proposal"` journal entry with its due-diligence results, not
  traded, until a human adds and verifies it.

## 5. Generate signals

For each watchlist token with enough price history, compute a signal using
`backtest/strategies.py`'s `adaptive_ensemble` (preferred once it has a
non-negative, low-overfit-risk out-of-sample result on file -- see
`.claude/skills/backtest-strategy`) or another strategy documented in
`docs/STRATEGY.md` as validated. **Only use a strategy with a non-negative
out-of-sample (`--walk-forward`) backtest result on file in
`backtest/results/`** for a similar coin/timeframe -- if none exists, run
the backtest skill first, or hold.

Combine signals across strategies conservatively where more than one is in
play: a "buy" wants agreement (or at least no direct contradiction); a
single strategy's sell/stop-loss/take-profit trigger is always enough to
exit, since protecting capital matters more than confirmation.

## 6. Position sizing & risk checks

Before proposing any trade, check ALL of:

- [ ] Token is on `config/watchlist.yaml` with `verified: true` (or this is an exit of an existing position)
- [ ] `max_concurrent_positions` not exceeded (for a new entry)
- [ ] `max_meme_positions` not exceeded (for a new meme-category entry)
- [ ] `max_same_ecosystem_positions` not exceeded
- [ ] `max_non_stable_exposure_fraction` not exceeded after this trade
- [ ] Regime filter allows new entries (step 3)
- [ ] Base position size = `min(max_position_usd, portfolio_value_usd * max_position_fraction) * category_multiplier`
      (category multipliers in `config/watchlist.yaml` -- meme is intentionally smaller: a real chance to make money on a
      meme coin doesn't require betting a large fraction of the account on it)
- [ ] Volatility-scaled size = `base_size * clamp(target_daily_volatility_pct / realized_20d_volatility_pct, volatility_size_min_mult, volatility_size_max_mult)`,
      and this is `>= min_trade_usd` (else skip -- too small to be worth fees)
- [ ] Adding this position keeps `sum(position_size_usd * stop_loss_pct) / portfolio_value_usd <= max_portfolio_heat_pct`
      (portfolio heat -- compute across ALL open positions including this candidate)
- [ ] Today's cumulative trade count < `max_daily_trade_count`
- [ ] Today's cumulative trade volume + this trade < `max_daily_volume_usd`
- [ ] At least `min_hours_between_trades_same_token` since the last trade in this token
- [ ] Quoted slippage <= `max_slippage_bps` and price impact <= `max_price_impact_bps`
      (get a quote first, e.g. via `buy_token` in quote-only/simulate mode, before executing)

If any check fails, do not trade that signal -- log why and move on.

## 7. Execute

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

## 8. Log

Append one entry to `journal/trades.jsonl` per `journal/README.md`'s format,
covering every token considered this cycle (not just ones traded), the
risk-check results (including portfolio heat and regime state), and the
action taken (including explicit `"hold"` entries). Update
`state/circuit_breaker.json`'s `peak_portfolio_value_usd` if applicable.

## 9. Report

Give the user a short summary: portfolio value before/after, what was
traded (if anything) and why, current open positions, current regime state
and portfolio heat, and whether anything needs their attention (a circuit
breaker trip, a proposed new watchlist token -- meme or otherwise, a
repeated execution anomaly).
