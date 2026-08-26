# Claude Agent Trader

An autonomous crypto trading agent for the **Kraken exchange**, wired up to
[Kraken CLI](https://github.com/krakenfx/kraken-cli)'s official, open-source
MCP server so Claude can research, discover, decide, size, and execute
trades directly through a real Kraken account -- with live pair discovery,
backtesting, hard risk limits, fee-aware execution, and idle-capital yield
built in.

This project originally ran on Solana through Phantom's MCP server. It has
been fully migrated to Kraken: Phantom requires a **local browser sign-in**
that fundamentally cannot complete in a cloud/remote Claude Code session,
which made autonomous cloud-hosted trading a dead end on that stack.
Kraken's API authenticates with a plain **API key + secret** -- no browser
step, in principle -- and running through Kraken opens up things the old
stack had no equivalent of: a disclosed, optimizable maker/taker fee
schedule, and a native yield product (Earn) for cash sitting idle between
trades. See [`docs/STRATEGY.md`](docs/STRATEGY.md) for the full "how to
actually make money on Kraken" reasoning.

There is no hand-maintained pair list: every cycle, the agent asks Kraken's
own public market data "what's actually tradeable and liquid right now"
(every listed pair's live 24h volume and bid/ask spread) and runs every
candidate through automated liquidity/spread/listing-status checks before
anything is ever eligible to trade. Unlike a permissionless DEX, Kraken's
own listing review is the trust anchor -- there's no on-chain rug-pull
surface to check -- so what still needs live, per-cycle checking is purely
execution quality: liquidity and spread, both of which move fast.

Starting point: a small, deliberately-at-risk account -- fund it with
whatever amount you actually intend to risk, there's no fixed minimum -- that
the agent trades autonomously, aiming to grow it over time while a
circuit-breaker and per-trade risk caps (sized as a percentage of whatever
you actually fund, not a hardcoded dollar figure) bound the downside from
bugs or bad signals.

**This is real money and real, fully autonomous trading. Read
[`docs/STRATEGY.md`](docs/STRATEGY.md) and
[`config/risk.yaml`](config/risk.yaml) before funding the account.**

## Quick start

1. **Set up Kraken CLI** (install the CLI, create a least-privilege API
   key, set credentials): [`docs/KRAKEN_CLI_SETUP.md`](docs/KRAKEN_CLI_SETUP.md)
2. **Verify the settlement assets and risk settings**:
   [`config/core_assets.yaml`](config/core_assets.yaml),
   [`config/discovery.yaml`](config/discovery.yaml),
   [`config/risk.yaml`](config/risk.yaml)
3. **Try live discovery** to see the liquidity/spread pipeline in action:
   ```
   python3 research/discover_candidates.py
   ```
4. **Backtest before going live**:
   [`.claude/skills/backtest-strategy`](.claude/skills/backtest-strategy/SKILL.md)
   ```
   python3 backtest/backtest_all.py          # everything currently eligible + BTC/ETH, one report
   ```
   or single-pair:
   ```
   python3 backtest/fetch_history.py --pair XBTUSD --days 180
   python3 backtest/run_backtest.py --coin XBTUSD --days 180 --strategy all --walk-forward
   ```
   `--walk-forward` runs an out-of-sample train/test split and flags overfit
   risk -- prefer it over a plain in-sample run before trusting a result.
5. **Paper trade** the full pipeline with zero financial risk (works even
   without a Kraken API key configured, as long as `api.kraken.com` is
   reachable):
   [`.claude/skills/paper-trade-cycle`](.claude/skills/paper-trade-cycle/SKILL.md)
   ```
   python3 paper_trading/run_paper_cycle.py
   ```
   Run this repeatedly (e.g. via `/loop`) to build a real track record before
   trusting the system with actual money.
6. **Run one real trade cycle manually**, then automate:
   [`docs/RUNBOOK.md`](docs/RUNBOOK.md)

## Testing

```
python3 -m unittest discover -s tests -v
```

Unit tests (stdlib `unittest`, no extra dependency) cover the strategy math,
the backtest engine's simulation mechanics, and the discovery pipeline's
liquidity/spread/tier logic against synthetic data -- fast, deterministic,
no network calls. They run automatically on every push/PR via
[`.github/workflows/tests.yml`](.github/workflows/tests.yml). This is one
layer of a four-layer validation approach, each catching a different kind of
mistake before it costs real money:

1. **Unit tests** -- is the arithmetic right? (e.g. does a fee actually
   reduce the recorded return; does a flat/no-movement price series get
   read as neutral instead of "overbought" -- a real edge case these tests
   caught and fixed during the project's original development, see
   `backtest/strategies.py`'s `rsi()`)
2. **Backtests** (`--walk-forward`) -- does a strategy have real, out-of-sample edge?
3. **Paper trading** -- does the *whole pipeline* (discovery + regime +
   sizing + risk) behave sensibly against live data, with simulated fills?
4. **Live** -- real money, only after the first three build confidence.

## How it works

```
research/discover_candidates.py ──▶ Kraken AssetPairs + Ticker (find + liquidity/spread audit)
         │
         ▼
config/discovery.yaml (thresholds) ──┐
config/risk.yaml (position/exit rules)├──▶ .claude/skills/trade-cycle ──▶ Kraken CLI MCP ──▶ Kraken exchange
state/circuit_breaker.json           │            │
backtest/strategies.py ──────────────┘            ▼
                                       journal/trades.jsonl (audit log)
```

Each `trade-cycle` run: checks the kill switch, circuit breaker, and market
regime; runs (or reuses a fresh) live discovery pass to get this cycle's
liquidity/spread-checked candidate list; generates signals from a
regime-aware, backtested strategy ensemble; sizes any resulting trade
against hard caps (volatility-scaled position size, portfolio heat, daily
volume, spread, order-book depth); executes through Kraken (preferring
maker/limit orders to minimize fees); optionally allocates idle cash to
Kraken's flexible Earn product; and logs everything, including every
rejected candidate and why. See
[`.claude/skills/trade-cycle/SKILL.md`](.claude/skills/trade-cycle/SKILL.md)
for the full step-by-step and
[`docs/STRATEGY.md`](docs/STRATEGY.md) for the complete "how to make money
on Kraken" reasoning, including fee-aware execution and idle-capital yield.

## Safety model

- **Kill switch**: `config/risk.yaml`'s `enabled: false` stops all trading
  immediately.
- **Circuit breaker**: auto-trips and halts new trades if the portfolio
  drops to a configured floor or a large single-day drawdown.
- **Discovery + automated liquidity gate**: nothing is tradeable unless it's
  a settlement currency (USD/USDT) or passed `research/discover_candidates.py`'s
  live checks this cycle (Kraken listing status, 24h quote volume, bid/ask
  spread) -- no human has to add a pair by name, and nothing gets a pass
  just because it looks promising.
- **Market regime filter**: blocks *new* entries (never exits) while the
  broader market (BTC) is trading below its own trend, so the agent isn't
  opening fresh risk into a market-wide downturn.
- **Volatility-scaled sizing + portfolio heat cap**: position size adjusts
  to each asset's own volatility, and total capital-at-risk across all open
  positions combined is capped independently of any single position's size.
- **Risk tiers**: every eligible pair is classified `blue_chip` /
  `established` / `emerging` from its own live Kraken volume/spread data,
  each with its own sizing multiplier -- smaller/thinner pairs get real but
  smaller exposure, not a blanket exclusion.
- **Concentration guardrails**: caps on emerging-tier positions,
  same-ecosystem positions, and non-stable exposure, so correlated pairs
  can't quietly become one oversized bet.
- **Position/volume/spread caps**: enforced every cycle before any trade,
  independent of what the strategy signal says.
- **Fee-aware execution**: prefers maker (limit) orders over taker (market)
  orders to capture Kraken's lower maker fee rate, checked against live
  spread and order-book depth before sizing -- see `docs/STRATEGY.md`
  "Fee-aware execution".
- **No leverage by default**: margin and perpetual futures are available on
  Kraken and exposed by the MCP server, but disabled in
  `config/risk.yaml`'s `asset_classes` unless a human explicitly turns them
  on -- this project's risk model is built and validated for unleveraged
  spot only.
- **Idle cash stays liquid**: Kraken Earn allocation is restricted to
  **flexible** (instant-unstake) products only -- never locked/bonded
  staking -- so it can never trap capital the circuit breaker needs to reach.
- **Least-privilege API key**: the trading key has no withdraw permission --
  funds can never leave the exchange account through anything this repo's
  skills do.

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for monitoring, pausing, and
circuit-breaker recovery.

## Repo layout

| Path | Purpose |
|---|---|
| `.mcp.json` | Registers Kraken CLI's built-in MCP server |
| `config/risk.yaml` | Kill switch + all risk limits |
| `config/core_assets.yaml` | USD/USDT -- the only non-discovered tradeable assets |
| `config/discovery.yaml` | Discovery sources, safety thresholds, tiers, pinned/denylist |
| `research/discover_candidates.py` | Live Kraken pair discovery + automated liquidity/spread scoring |
| `.claude/skills/trade-cycle/` | The autonomous trading loop |
| `.claude/skills/backtest-strategy/` | Strategy validation workflow |
| `.claude/skills/paper-trade-cycle/` | Zero-risk simulated trading (full pipeline, no Kraken account needed) |
| `backtest/` | Backtesting engine, strategies, single-pair CLI |
| `backtest/backtest_all.py` | Comprehensive backtest -- everything eligible + BTC/ETH, one ranked report |
| `paper_trading/run_paper_cycle.py` | Paper-trading simulator (real pipeline, simulated fills) |
| `tests/` | Unit tests (stdlib `unittest`) for strategies, engine, discovery, and paper-trading sizing logic |
| `.github/workflows/tests.yml` | CI -- runs `tests/` on every push/PR |
| `docs/KRAKEN_CLI_SETUP.md` | API key setup & funding |
| `docs/STRATEGY.md` | How to make money on Kraken: fees, Earn, discovery, strategy/risk reasoning |
| `docs/RUNBOOK.md` | Day-to-day operation |
| `docs/TERMUX_SETUP.md` | Running on Android via Termux |
| `journal/` | Append-only trade log (`trades.jsonl` real, `paper_trades.jsonl` simulated) |
| `state/circuit_breaker.json` | Circuit breaker status |
| `state/paper_portfolio.json` | Paper-trading simulated portfolio state |
