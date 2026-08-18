# Claude Agent Trader

An autonomous crypto trading agent for Solana, wired up to
[Phantom's official MCP server](https://docs.phantom.com/phantom-mcp-server)
so Claude can research, decide, size, and execute trades directly through a
dedicated Phantom-managed wallet -- with backtesting and hard risk limits
built in.

Starting point: a small, deliberately-at-risk account (recommended ~$50) that
the agent trades autonomously, aiming to grow it over time while a
circuit-breaker and per-trade risk caps bound the downside from bugs or bad
signals.

**This is real money and real, fully autonomous trading. Read
[`docs/STRATEGY.md`](docs/STRATEGY.md) and
[`config/risk.yaml`](config/risk.yaml) before funding the wallet.**

## Quick start

1. **Set up Phantom MCP** (local machine required -- browser auth):
   [`docs/PHANTOM_MCP_SETUP.md`](docs/PHANTOM_MCP_SETUP.md)
2. **Verify the watchlist** and its risk settings:
   [`config/watchlist.yaml`](config/watchlist.yaml),
   [`config/risk.yaml`](config/risk.yaml)
3. **Backtest before going live**:
   [`.claude/skills/backtest-strategy`](.claude/skills/backtest-strategy/SKILL.md)
   ```
   python3 backtest/fetch_history.py --coin solana --days 180
   python3 backtest/run_backtest.py --coin solana --days 180 --strategy all --walk-forward
   ```
   `--walk-forward` runs an out-of-sample train/test split and flags overfit
   risk -- prefer it over a plain in-sample run before trusting a result.
4. **Run one trade cycle manually**, then automate:
   [`docs/RUNBOOK.md`](docs/RUNBOOK.md)

## How it works

```
   docs/STRATEGY.md ──────────┐  (coin fundamentals, due-diligence, risk reasoning)
                               │
config/risk.yaml  ──┐         │
config/watchlist.yaml┤──▶ .claude/skills/trade-cycle ──▶ Phantom MCP ──▶ Solana
state/circuit_breaker.json    │        │
backtest/strategies.py ───────┘        ▼
                                journal/trades.jsonl (audit log)
```

Each `trade-cycle` run: checks the kill switch, circuit breaker, and market
regime; pulls live market data for watchlisted tokens (memecoins included,
gated the same as anything else); generates signals from a regime-aware,
backtested strategy ensemble; sizes any resulting trade against hard caps
(volatility-scaled position size, portfolio heat, daily volume, slippage,
liquidity, concentration); executes through Phantom; and logs everything.
See [`.claude/skills/trade-cycle/SKILL.md`](.claude/skills/trade-cycle/SKILL.md)
for the full step-by-step.

## Safety model

- **Kill switch**: `config/risk.yaml`'s `enabled: false` stops all trading
  immediately.
- **Circuit breaker**: auto-trips and halts new trades if the portfolio
  drops to a configured floor or a large single-day drawdown.
- **Watchlist + verification gate**: the agent can only ever buy tokens on
  `config/watchlist.yaml` that are also marked `verified: true`, each vetted
  against the due-diligence checklist in `docs/STRATEGY.md` (liquidity,
  mint/freeze authority, holder concentration, project legitimacy) --
  memecoins are explicitly in scope, but go through the same gate, with
  extra scrutiny since impersonator tokens are common.
- **Market regime filter**: blocks *new* entries (never exits) while the
  broader market is trading below its own trend, so the agent isn't opening
  fresh risk into a market-wide downturn.
- **Volatility-scaled sizing + portfolio heat cap**: position size adjusts
  to each asset's own volatility, and total capital-at-risk across all open
  positions combined is capped independently of any single position's size.
- **Concentration guardrails**: caps on meme positions, same-ecosystem
  positions, and non-stable exposure, so correlated tokens can't quietly
  become one oversized bet.
- **Position/volume/slippage caps**: enforced every cycle before any trade,
  independent of what the strategy signal says.
- **Separate wallet**: Phantom MCP issues the agent its own dedicated
  wallet, distinct from your personal Phantom wallet -- fund it with only
  what you intend to risk.

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for monitoring, pausing, and
circuit-breaker recovery.

## Repo layout

| Path | Purpose |
|---|---|
| `.mcp.json` | Registers the Phantom MCP server |
| `config/risk.yaml` | Kill switch + all risk limits |
| `config/watchlist.yaml` | Allow-listed tradeable tokens |
| `.claude/skills/trade-cycle/` | The autonomous trading loop |
| `.claude/skills/backtest-strategy/` | Strategy validation workflow |
| `backtest/` | Backtesting engine, strategies, CLI |
| `docs/PHANTOM_MCP_SETUP.md` | Wallet setup & funding |
| `docs/STRATEGY.md` | Coin fundamentals, due diligence, strategy/risk reasoning |
| `docs/RUNBOOK.md` | Day-to-day operation |
| `journal/` | Append-only trade log |
| `state/circuit_breaker.json` | Circuit breaker status |
