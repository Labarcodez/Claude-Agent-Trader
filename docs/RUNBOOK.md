# Runbook

## First-time setup

1. Follow `docs/PHANTOM_MCP_SETUP.md` end to end (local machine, browser
   auth, fund the agent's wallet with your $50).
2. Verify SOL and USDC's mint addresses in `config/core_assets.yaml` against
   an authoritative source before trusting them -- these are the only two
   tokens not covered by live discovery's automated checks.
3. Try discovery on its own first, to see the safety pipeline working before
   any money is at stake:
   ```
   python3 research/discover_candidates.py
   ```
   Read the eligible/rejected output and skim a few of the rejection reasons
   -- if the thresholds in `config/discovery.yaml` feel too strict or too
   loose for your taste, adjust them there (and keep
   `research/discover_candidates.py`'s CLI defaults in sync -- see that
   file's docstring).
4. Backtest, with `--walk-forward` to check for overfitting rather than
   trusting a single in-sample run:
   ```
   python3 backtest/fetch_history.py --coin solana --days 180
   python3 backtest/run_backtest.py --coin solana --days 180 --strategy all --walk-forward
   # repeat for bitcoin (the regime-filter reference coin), and for any
   # currently-eligible discovered tokens that have a CoinGecko id
   ```
   Only strategies with a non-negative **out-of-sample** result and low
   overfit risk should be relied on live (`docs/STRATEGY.md` "Judging a
   backtest").
5. Run one `trade-cycle` manually (ask Claude Code, in this repo, to run the
   `trade-cycle` skill once) and read the journal entry it produces in
   `journal/trades.jsonl` before automating anything.

## Running autonomously

Once you've validated a manual cycle looks right, use `/loop` (see the
`loop` skill) to re-run `trade-cycle` on an interval, e.g.:

```
/loop 4h .claude/skills/trade-cycle
```

Pick an interval that matches the strategy's timeframe -- these strategies
use daily-scale signals (SMA10/30, RSI14), so running every few minutes
adds cost without adding information. Every 4-12 hours is more sensible for
a $50 account than every few minutes.

Alternatively, use a Claude Code Remote Routine / cron trigger bound to a
session running on a machine with Phantom already authenticated, if you want
it to run without you keeping a local session open.

## Monitoring

- `journal/trades.jsonl` -- every cycle's decisions and actions.
- `state/circuit_breaker.json` -- current trip status and peak portfolio value.
- Ask Claude at any time: "check the trading wallet status" -- it should read
  the journal + circuit breaker file + live Phantom balances and summarize.

## Stopping / pausing

- **Immediate stop**: set `enabled: false` in `config/risk.yaml`. The
  `trade-cycle` skill checks this first, every cycle, before touching the
  wallet.
- **Stop the loop**: cancel the `/loop` or Routine driving `trade-cycle`.
- The agent's wallet is separate from your personal wallet (see setup doc),
  so stopping the agent never affects anything else in your Phantom account.

## Recovering from a circuit breaker trip

`state/circuit_breaker.json`'s `tripped: true` means the portfolio hit the
floor (`circuit_breaker_floor_usd`) or a large single-day drawdown. Before
resuming:

1. Read the trip reason and the journal entries leading up to it -- was it a
   strategy failure, a bad fill, a liquidity problem, or a bug?
2. Decide whether to adjust `config/risk.yaml` (smaller positions, different
   strategy, lower floor) based on what you learn.
3. Manually reset `state/circuit_breaker.json`'s `tripped` to `false` (and
   clear `reason`/`portfolio_value_usd_at_trip`) only after you've addressed
   the cause -- this file is intentionally not something the skill resets on
   its own.
4. Confirm `enabled: true` in `config/risk.yaml`.
5. Run one manual `trade-cycle` and check the result before resuming any
   loop/schedule.

## Withdrawing funds

Ask Claude to use the Phantom MCP transfer tool to send SOL/tokens from the
agent's wallet to your personal wallet address at any time -- this doesn't
require the trading loop to be stopped first, but stopping it
(`enabled: false`) first avoids a race with an in-flight autonomous trade.
