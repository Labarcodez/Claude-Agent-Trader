# Runbook

## First-time setup

1. Follow `docs/KRAKEN_CLI_SETUP.md` end to end: install kraken-cli, create
   a least-privilege API key, set credentials, fund the account with
   whatever amount you intend to risk -- there's no fixed minimum.
2. Verify `config/core_assets.yaml`'s settlement-currency notes make sense
   for your jurisdiction (USD fiat funding availability varies).
3. Try discovery on its own first, to see the liquidity/spread pipeline
   working before any money is at stake:
   ```
   python3 research/discover_candidates.py
   ```
   Read the eligible/rejected output and skim a few of the rejection reasons
   -- if the thresholds in `config/discovery.yaml` feel too strict or too
   loose for your taste, adjust them there (and keep
   `research/discover_candidates.py`'s CLI defaults in sync -- see that
   file's docstring).
4. Backtest everything currently eligible in one pass, with walk-forward to
   check for overfitting rather than trusting a single in-sample run:
   ```
   python3 backtest/backtest_all.py
   ```
   Only strategies with a non-negative **out-of-sample** result and low
   overfit risk should be relied on live (`docs/STRATEGY.md` "Judging a
   backtest"). Read the drawdown column too, not just returns -- expect
   emerging-tier pairs to show much bigger drawdowns than BTC/ETH even when
   "winning"; that's the evidence behind the tier sizing in
   `config/discovery.yaml`, not just a theoretical caution.
5. **Paper trade** before ever running the real thing -- this can even be
   done in a session with no Kraken account/API key configured at all (see
   "Paper trading before going live" below).
6. Run one real `trade-cycle` manually (ask Claude Code, in this repo, to
   run the `trade-cycle` skill once) and read the journal entry it produces
   in `journal/trades.jsonl` before automating anything.

## Paper trading before going live

`paper_trading/run_paper_cycle.py` runs the exact same discovery, regime
filter, and strategy code the live agent uses, against real live Kraken
market data, but simulates fills against `state/paper_portfolio.json`
instead of calling any Kraken MCP trade tool. No real money, no API key
required -- just outbound access to Kraken's public REST endpoints.

```
python3 paper_trading/run_paper_cycle.py
```

First run creates a simulated $500 portfolio (or whatever
`--starting-capital-usd` you pass -- there's no fixed/required amount for
real trading either, this default is just a reasonable illustrative size).
Run it again later and it picks up from where it left off. Run it
repeatedly -- manually, or via `/loop .claude/skills/paper-trade-cycle` --
to build an actual track record over days/weeks:

- `journal/paper_trades.jsonl` accumulates every cycle's discovery stats,
  actions, and portfolio value -- read it the same way you'd read the real
  trade journal, per `journal/README.md`.
- `state/paper_portfolio.json`'s `closed_trades` list gives you a real win
  rate / return distribution to look at before trusting the live system.
- Use `--reset` to wipe paper state and start fresh, e.g. after changing
  `backtest/strategies.py` or `config/discovery.yaml`'s thresholds, so old
  and new paper results don't mix.

Kraken CLI also has its own built-in paper trading
(`kraken workspace create ... --mode paper`, `kraken paper buy/sell`) that
simulates real order mechanics against Kraken's live order book. That
validates order execution itself; `run_paper_cycle.py` validates the
higher-level discovery/strategy/sizing pipeline. Reasonable to use both.

A short paper-trading run is weak evidence, the same way a short backtest
window is -- treat a few days of paper cycles as "the pipeline didn't
obviously break," not as proof of an edge. See
`.claude/skills/paper-trade-cycle/SKILL.md` for what it simplifies vs. live
trading (no real order-book depth check, no daily cadence caps, no Earn
simulation) before over-trusting its results.

## Running autonomously

Once you've validated a manual cycle looks right, use `/loop` (see the
`loop` skill) to re-run `trade-cycle` on an interval, e.g.:

```
/loop 4h .claude/skills/trade-cycle
```

Pick an interval that matches the strategy's timeframe -- these strategies
use daily-scale signals (SMA10/30, RSI14), so running every few minutes
adds trading fees and cost without adding information. Every 4-12 hours is
more sensible than every few minutes.

Alternatively, use a Claude Code Remote Routine / cron trigger bound to a
session with Kraken credentials configured, if you want it to run without
you keeping a local session open -- unlike the project's original Phantom
setup, this doesn't require a session that's completed a local browser
sign-in, only one with `KRAKEN_API_KEY`/`KRAKEN_API_SECRET` set and network
access to `api.kraken.com` (see `docs/KRAKEN_CLI_SETUP.md` and
`.claude/skills/trade-cycle/SKILL.md` step 0 for the caveat on the latter).

## Monitoring

- `journal/trades.jsonl` -- every real cycle's decisions and actions.
- `journal/paper_trades.jsonl` / `state/paper_portfolio.json` -- same, for
  the paper-trading simulation (never mix these up with the real ones).
- `state/circuit_breaker.json` -- current trip status and peak portfolio value.
- Ask Claude at any time: "check the trading account status" -- it should
  read the journal + circuit breaker file + live Kraken balances (`kraken
  balance`, `kraken open-orders`, `kraken positions`) and summarize.
- `kraken trades-history` / `kraken ledgers` give a Kraken-side view
  independent of this repo's own journal -- useful for cross-checking that
  the journal actually matches what the exchange thinks happened.

## Stopping / pausing

- **Immediate stop**: set `enabled: false` in `config/risk.yaml`. The
  `trade-cycle` skill checks this first, every cycle, before touching the
  account.
- **Stop the loop**: cancel the `/loop` or Routine driving `trade-cycle`.
- **Cancel all open orders immediately** (independent of stopping the
  loop): `kraken order cancel-all`. Kraken CLI also supports a dead-man's
  switch (`kraken order cancel-after <seconds>`) that auto-cancels
  everything if the CLI doesn't reset the timer -- useful extra protection
  if a scheduled cycle might stop running unexpectedly (a crashed loop, a
  killed session) while orders are still resting.
- The trading account's API key is scoped read/trade-only (no withdraw
  permission -- see `docs/KRAKEN_CLI_SETUP.md` step 2), so stopping the
  agent never risks funds leaving the account even if something goes wrong.

## Recovering from a circuit breaker trip

`state/circuit_breaker.json`'s `tripped: true` means the portfolio hit the
floor (`circuit_breaker_floor_usd`) or a large single-day drawdown. Before
resuming:

1. Read the trip reason and the journal entries leading up to it -- was it a
   strategy failure, a bad fill, a liquidity/spread problem, or a bug?
2. If any cash was allocated to Kraken Earn (`config/risk.yaml`'s
   `kraken_earn`), deallocate it back to spendable balance -- it should
   already be flexible-only and instant to unstake (see
   `docs/STRATEGY.md` "Idle-capital yield"); confirm it actually landed
   back in the balance before treating the account as liquid again.
3. Decide whether to adjust `config/risk.yaml` (smaller positions, different
   strategy, lower floor) based on what you learn.
4. Manually reset `state/circuit_breaker.json`'s `tripped` to `false` (and
   clear `reason`/`portfolio_value_usd_at_trip`) only after you've addressed
   the cause -- this file is intentionally not something the skill resets on
   its own.
5. Confirm `enabled: true` in `config/risk.yaml`.
6. Run one manual `trade-cycle` and check the result before resuming any
   loop/schedule.

## Withdrawing funds

The trading API key used by `.mcp.json` deliberately has no withdraw
permission (see `docs/KRAKEN_CLI_SETUP.md` step 2) -- withdrawals are a
manual, out-of-band action you take directly on kraken.com or via a
separate, deliberately-created key with that permission, not something to
ask the agent to do through this repo's skills. Stopping the trading loop
first (`enabled: false`) avoids a race with an in-flight autonomous trade,
but isn't strictly required to withdraw.
