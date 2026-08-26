# Runbook

## First-time setup

1. Follow `docs/KRAKEN_SETUP.md` end to end (create a Kraken account, fund
   it with whatever amount you intend to risk -- there's no fixed minimum --
   create a scoped API key, fill in `.env`).
2. Try discovery on its own first, to see the safety pipeline working before
   any money is at stake:
   ```
   python3 research/discover_candidates.py
   ```
   Read the eligible/rejected output and skim a few of the rejection reasons
   -- if the thresholds in `config/discovery.yaml` feel too strict or too
   loose for your taste, adjust them there (and keep
   `research/discover_candidates.py`'s CLI defaults in sync -- see that
   file's docstring). No API key needed for this step.
3. Backtest everything currently eligible in one pass, with walk-forward to
   check for overfitting rather than trusting a single in-sample run:
   ```
   python3 backtest/backtest_all.py
   ```
   Only strategies with a non-negative **out-of-sample** result and low
   overfit risk should be relied on live (`docs/STRATEGY.md` "Judging a
   backtest"). Read the drawdown column too, not just returns -- expect
   emerging-tier pairs to show much bigger drawdowns than BTC/ETH even when
   "winning"; that's the evidence behind the tier sizing in
   `config/discovery.yaml`, not just a theoretical caution. No API key
   needed for this step either.
4. **Paper trade** before ever running the real thing -- this can even be
   done in a session with no Kraken API key configured at all (see "Paper
   trading before going live" below).
5. Run one real `trade-cycle` manually (ask Claude Code, in this repo, to
   run the `trade-cycle` skill once) and read the journal entry it produces
   in `journal/trades.jsonl` before automating anything.

## Paper trading before going live

`paper_trading/run_paper_cycle.py` runs the exact same discovery, regime
filter, and strategy code the live agent uses, against real live Kraken
market data, but simulates fills against `state/paper_portfolio.json`
instead of placing any real order. No real money, no Kraken API key
required.

```
python3 paper_trading/run_paper_cycle.py
```

First run creates a simulated $50 portfolio (or whatever `--starting-capital-usd`
you pass). Run it again later and it picks up from where it left off. Run it
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

A short paper-trading run is weak evidence, the same way a short backtest
window is -- treat a few days of paper cycles as "the pipeline didn't
obviously break," not as proof of an edge. See
`.claude/skills/paper-trade-cycle/SKILL.md` for what it simplifies vs. live
trading (no real order validation beyond discovery's own screening, no
daily cadence caps) before over-trusting its results.

## Reviewing performance (learning over time)

Periodically -- weekly, or every ~50-100 cycles -- ask Claude to run
`.claude/skills/review-trading-performance`, or directly:
```
python3 scripts/analyze_journal.py
```
This breaks down closed trades by exit reason, tier, strategy, and pair,
and surfaces which `not_traded` rejection reasons dominate -- the same
"mine the journal" habit that's already found real bugs in this project
before (see git history / code comments for examples). It never edits
`config/risk.yaml` or `config/discovery.yaml` on its own -- it turns a
genuine, sufficiently-sampled pattern into a specific proposed change for
you to approve, the same review discipline as any other config change
(CLAUDE.md rule 7).

## Running autonomously

Once you've validated a manual cycle looks right, use `/loop` (see the
`loop` skill) to re-run `trade-cycle` on an interval, e.g.:

```
/loop 4h .claude/skills/trade-cycle
```

Pick an interval that matches the strategy's timeframe -- these strategies
use daily-scale signals (SMA10/30, RSI14), so running every few minutes
adds cost without adding information. Every 4-12 hours is more sensible for
a small account than every few minutes.

Alternatively, use a Claude Code Remote Routine / cron trigger bound to a
session with `KRAKEN_API_KEY`/`KRAKEN_API_SECRET` set, if you want it to
run without you keeping a local session open -- unlike the old Phantom
setup, this works from a cloud/remote session too (Kraken's API needs no
local browser), though live execution still always requires a human to run
the `--execute` step (CLAUDE.md rule 1).

## Monitoring

- `journal/trades.jsonl` -- every real cycle's decisions and actions.
- `journal/paper_trades.jsonl` / `state/paper_portfolio.json` -- same, for
  the paper-trading simulation (never mix these up with the real ones).
- `state/circuit_breaker.json` -- current trip status and peak portfolio value.
- Ask Claude at any time: "check the trading account status" -- it should
  read the journal + circuit breaker file + live Kraken balances
  (`kraken.client.balance()`) and summarize.

## Stopping / pausing

- **Immediate stop**: set `enabled: false` in `config/risk.yaml`. The
  `trade-cycle` skill checks this first, every cycle, before touching the
  account.
- **Stop the loop**: cancel the `/loop` or Routine driving `trade-cycle`.

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

Withdrawals are always something you do yourself, directly in Kraken's own
web UI or app -- the API key this project uses should never have Withdraw
Funds permission enabled in the first place (see `docs/KRAKEN_SETUP.md`), so
there's no agent-driven withdrawal path to ask for here by design.
