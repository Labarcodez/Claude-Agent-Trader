---
name: paper-trade-cycle
description: Run one simulated (paper) trading cycle -- the same discovery, regime filter, strategy, and risk logic as trade-cycle, but against a simulated portfolio instead of the real Kraken account. No Kraken MCP connection or API key required. Use to validate the pipeline, build a track record before trusting it with real money, or whenever the user asks to paper trade, dry-run, or simulate a trading cycle.
---

# Paper trade cycle

Runs `paper_trading/run_paper_cycle.py`, which reuses the exact same
discovery (`research/discover_candidates.py`) and strategy
(`backtest/strategies.py`) code the live `trade-cycle` skill is documented
to use, against real live Kraken market data -- but simulates fills against
a local paper portfolio (`state/paper_portfolio.json`) instead of calling
any Kraken MCP trade tool. This works in ANY session that can reach Kraken's
public REST endpoints (`api.kraken.com`), since it only reads public market
data -- no API key, no MCP server, no account needed.

**Note:** if this session's network egress policy blocks `api.kraken.com`
(some cloud/remote environments do -- see `.claude/skills/trade-cycle`'s
step 0), this script will fail to fetch data the same way the live skill
would, even though no account is involved. That's a network-policy
limitation, not a live-trading-specific one -- say so plainly rather than
fabricating a cycle result.

Kraken CLI (kraken-cli) also ships its own built-in paper trading engine
(`kraken workspace create ... --mode paper`, `kraken paper buy/sell`), which
simulates real order mechanics against Kraken's live order book. That's a
complementary, lower-level check on order execution itself; this script is
the higher-level pipeline validator for the discovery → regime → strategy →
sizing → risk chain the live skill actually runs on top of. Using both
before trusting the system live is reasonable, not redundant.

## When to use this instead of `trade-cycle`

- Before ever running `trade-cycle` for real, to see the full pipeline
  (discovery → regime filter → signals → sizing → risk checks → simulated
  fills) behave against live data with zero financial risk.
- To build a track record over days/weeks (run repeatedly via `/loop`) and
  decide, from real results, whether the strategy/thresholds are worth
  trusting with real money.
- Any time a Kraken MCP account connection isn't set up (e.g. no API key
  configured yet) but someone wants to see the decision-making work anyway.

## Running it

```
python3 paper_trading/run_paper_cycle.py
```

First run auto-creates `state/paper_portfolio.json` at the starting capital
in `config/risk.yaml`'s spirit (default $500, override with
`--starting-capital-usd` -- there's no fixed/required amount for real
trading either, this default is just a reasonable "small account" example).
Subsequent runs continue from that state. Use `--reset` to wipe it and start
over (e.g. after changing strategy or risk parameters, so old paper results
don't mix with new ones).

Read `paper_trading/run_paper_cycle.py`'s module docstring for the specific
ways it simplifies vs. live trading (no real order-book depth check, no
daily trade-count/volume cadence caps, no Earn/idle-cash yield simulation)
before treating its results as a precise forecast of live behavior -- it's a
pipeline validator, not a perfect simulator.

## After running

1. Report the cycle summary the script prints: discovery stats, regime
   state, any buy/sell actions and why, portfolio value before/after.
2. `journal/paper_trades.jsonl` accumulates a full history, separate from
   `journal/trades.jsonl` (real trades) -- never mix the two when reasoning
   about actual account performance.
3. If the paper circuit breaker trips (`state/paper_portfolio.json`'s
   `circuit_breaker.tripped`), treat it the same as a live trip would be
   treated -- investigate before running `--reset` and continuing, per
   `docs/RUNBOOK.md` "Recovering from a circuit breaker trip" (same
   reasoning applies, different file).
4. If the user is deciding whether to go live, summarize the paper track
   record honestly: total return, number of cycles/trades, win rate from
   `closed_trades` in the state file -- and note that a short paper history
   is weak evidence either way, same caveat as a backtest (see
   `docs/STRATEGY.md` "Judging a backtest").
