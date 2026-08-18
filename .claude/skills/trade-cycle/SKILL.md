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
3. Read `config/watchlist.yaml` -- this is the only set of tokens you may
   ever buy.

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

## 3. Research

For every token on the watchlist (plus any currently open position even if
since removed from the watchlist, so it can still be exited):

- Pull price, 24h change, 24h volume, and liquidity. Prefer a market-data MCP
  tool if one is connected in this session (e.g. CoinGecko); otherwise fall
  back to a direct HTTPS call to `https://api.coingecko.com/api/v3/...`
  (no key required) via whatever fetch tool is available.
- Confirm `min_liquidity_usd` and reasonable 24h volume are still met --
  liquidity can evaporate; re-check every cycle, don't trust the watchlist
  note alone.
- For **any token not already on the watchlist** that research surfaces as
  interesting (e.g. a trending token), do NOT trade it. Run it through the
  due-diligence checklist in `docs/STRATEGY.md` and log a `"proposal"` entry
  in the journal recommending the human add it to `config/watchlist.yaml`.
  `require_watchlist_membership: true` in risk.yaml means this is a hard
  rule, not a suggestion.

## 4. Generate signals

For each watchlist token with enough price history, compute signals using
the strategies in `backtest/strategies.py` (same logic, applied to live data)
or a strategy documented in `docs/STRATEGY.md` as validated. **Only use a
strategy that has a non-negative backtest result on file in
`backtest/results/`** for a similar coin/timeframe -- if none exists, run
`backtest/fetch_history.py` + `backtest/run_backtest.py` first, or hold.

Combine signals across strategies conservatively: a "buy" requires agreement
(or at least no direct contradiction) from more than one signal source where
possible; a single strategy's "sell"/stop-loss/take-profit trigger is always
enough to exit, since protecting capital matters more than confirmation.

## 5. Position sizing & risk checks

Before proposing any trade, check ALL of:

- [ ] Token is on `config/watchlist.yaml` (or this is an exit of an existing position)
- [ ] `max_concurrent_positions` not exceeded (for a new entry)
- [ ] Position size = `min(max_position_usd, portfolio_value_usd * max_position_fraction * category_multiplier)`,
      and >= `min_trade_usd` (else skip -- too small to be worth fees)
- [ ] Today's cumulative trade count < `max_daily_trade_count`
- [ ] Today's cumulative trade volume + this trade < `max_daily_volume_usd`
- [ ] At least `min_hours_between_trades_same_token` since the last trade in this token
- [ ] Quoted slippage <= `max_slippage_bps` and price impact <= `max_price_impact_bps`
      (get a quote first, e.g. via `buy_token` in quote-only/simulate mode, before executing)

If any check fails, do not trade that signal -- log why and move on.

## 6. Execute

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

## 7. Log

Append one entry to `journal/trades.jsonl` per `journal/README.md`'s format,
covering every token considered this cycle (not just ones traded), the
risk-check results, and the action taken (including explicit `"hold"`
entries). Update `state/circuit_breaker.json`'s `peak_portfolio_value_usd`
if applicable.

## 8. Report

Give the user a short summary: portfolio value before/after, what was
traded (if anything) and why, current open positions, and whether anything
needs their attention (a circuit breaker trip, a proposed new watchlist
token, a repeated execution anomaly).
