# Claude Agent Trader

An autonomous crypto trading agent for Solana, wired up to
[Phantom's official MCP server](https://docs.phantom.com/phantom-mcp-server)
so Claude can research, discover, decide, size, and execute trades directly
through a dedicated Phantom-managed wallet -- with live token discovery,
backtesting, and hard risk limits built in.

There is no hand-maintained token list: every cycle, the agent asks live
market data "what's worth looking at right now" (Jupiter's trending/organic/
newest-pool feeds) and runs every candidate through automated, on-chain-backed
safety checks (liquidity, holder concentration, mint/freeze authority,
pool age, confirmed-rug history) before anything is ever eligible to trade --
memecoins included, sized for their volatility rather than excluded.

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
2. **Verify the core assets and risk settings**:
   [`config/core_assets.yaml`](config/core_assets.yaml),
   [`config/discovery.yaml`](config/discovery.yaml),
   [`config/risk.yaml`](config/risk.yaml)
3. **Try live discovery** to see the safety pipeline in action:
   ```
   python3 research/discover_candidates.py
   ```
4. **Backtest before going live**:
   [`.claude/skills/backtest-strategy`](.claude/skills/backtest-strategy/SKILL.md)
   ```
   python3 backtest/fetch_history.py --coin solana --days 180
   python3 backtest/run_backtest.py --coin solana --days 180 --strategy all --walk-forward
   ```
   `--walk-forward` runs an out-of-sample train/test split and flags overfit
   risk -- prefer it over a plain in-sample run before trusting a result.
5. **Run one trade cycle manually**, then automate:
   [`docs/RUNBOOK.md`](docs/RUNBOOK.md)

## How it works

```
research/discover_candidates.py ──▶ Jupiter Tokens API v2 (find + on-chain audit)
         │                       ──▶ RugCheck.xyz (confirmed-rug cross-check)
         ▼
config/discovery.yaml (thresholds) ──┐
config/risk.yaml (position/exit rules)├──▶ .claude/skills/trade-cycle ──▶ Phantom MCP ──▶ Solana
state/circuit_breaker.json           │            │
backtest/strategies.py ──────────────┘            ▼
                                       journal/trades.jsonl (audit log)
```

Each `trade-cycle` run: checks the kill switch, circuit breaker, and market
regime; runs (or reuses a fresh) live discovery pass to get this cycle's
safety-checked candidate list; generates signals from a regime-aware,
backtested strategy ensemble; sizes any resulting trade against hard caps
(volatility-scaled position size, portfolio heat, daily volume, slippage,
liquidity, concentration); executes through Phantom; and logs everything,
including every rejected candidate and why. See
[`.claude/skills/trade-cycle/SKILL.md`](.claude/skills/trade-cycle/SKILL.md)
for the full step-by-step and
[`docs/STRATEGY.md`](docs/STRATEGY.md) "Autonomous discovery" for a real
worked example of the safety pipeline rejecting and accepting live candidates.

## Safety model

- **Kill switch**: `config/risk.yaml`'s `enabled: false` stops all trading
  immediately.
- **Circuit breaker**: auto-trips and halts new trades if the portfolio
  drops to a configured floor or a large single-day drawdown.
- **Discovery + automated safety gate**: nothing is tradeable unless it's
  SOL/USDC or passed `research/discover_candidates.py`'s live checks this
  cycle (liquidity, holder count, real-vs-wash-traded volume, mint/freeze
  authority disabled, pool age, confirmed-rug cross-check) -- no human has
  to add a token by name, and nothing gets a pass just because it looks
  promising. Memecoins are explicitly in scope, gated the same as anything
  else, with extra scrutiny since impersonator tokens are common.
- **Market regime filter**: blocks *new* entries (never exits) while the
  broader market is trading below its own trend, so the agent isn't opening
  fresh risk into a market-wide downturn.
- **Volatility-scaled sizing + portfolio heat cap**: position size adjusts
  to each asset's own volatility, and total capital-at-risk across all open
  positions combined is capped independently of any single position's size.
- **Risk tiers**: every eligible token is classified `blue_chip` /
  `established` / `emerging` from its own live data (market cap, holders,
  verification), each with its own sizing multiplier -- newer/smaller/meme
  tokens get real but smaller exposure, not a blanket exclusion.
- **Concentration guardrails**: caps on emerging-tier positions,
  same-ecosystem positions, and non-stable exposure, so correlated tokens
  can't quietly become one oversized bet.
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
| `config/core_assets.yaml` | SOL/USDC -- the only non-discovered tradeable assets |
| `config/discovery.yaml` | Discovery sources, safety thresholds, tiers, pinned/denylist |
| `research/discover_candidates.py` | Live token discovery + automated safety scoring |
| `.claude/skills/trade-cycle/` | The autonomous trading loop |
| `.claude/skills/backtest-strategy/` | Strategy validation workflow |
| `backtest/` | Backtesting engine, strategies, CLI |
| `docs/PHANTOM_MCP_SETUP.md` | Wallet setup & funding |
| `docs/STRATEGY.md` | Coin fundamentals, autonomous discovery, strategy/risk reasoning |
| `docs/RUNBOOK.md` | Day-to-day operation |
| `journal/` | Append-only trade log |
| `state/circuit_breaker.json` | Circuit breaker status |
