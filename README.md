# Claude Agent Trader

An autonomous crypto trading agent for [Kraken](https://www.kraken.com), so
Claude can research, discover, decide, size, and *propose* trades against a
Kraken spot account -- with live pair discovery, backtesting, and hard risk
limits built in. A human always runs the actual order-placement step
themselves (see "Safety model" below) -- Claude never places a real order.

There is no hand-maintained pair list: every cycle, the agent asks Kraken's
own public API "what USD pairs do you actually list right now" and runs
every one through automated liquidity/spread/tradability checks before
anything is ever eligible to trade -- lower-cap/lower-volume pairs included,
sized for their volatility rather than excluded.

Starting point: a small, deliberately-at-risk account -- fund it with
whatever amount you actually intend to risk, there's no fixed minimum -- that
the agent trades, aiming to grow it over time while a circuit-breaker and
per-trade risk caps (sized as a percentage of whatever you actually fund,
not a hardcoded dollar figure) bound the downside from bugs or bad signals.

**This is real money. Read [`docs/STRATEGY.md`](docs/STRATEGY.md) and
[`config/risk.yaml`](config/risk.yaml) before funding the account.**

## Quick start

1. **Set up a Kraken API key**: [`docs/KRAKEN_SETUP.md`](docs/KRAKEN_SETUP.md)
   -- not required for steps 2-5 below (discovery/backtesting/paper trading
   are all public-data-only).
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
   python3 backtest/backtest_all.py          # everything currently eligible + BTC/ETH, one report
   ```
   or single-pair:
   ```
   python3 backtest/fetch_history.py --kraken-pair XBTUSD --days 180
   python3 backtest/run_backtest.py --coin kraken_XBTUSD --days 180 --strategy all --walk-forward
   ```
   `--walk-forward` runs an out-of-sample train/test split and flags overfit
   risk -- prefer it over a plain in-sample run before trusting a result.
5. **Paper trade** the full pipeline with zero financial risk (works even
   without a Kraken API key configured):
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
the backtest engine's simulation mechanics, the Kraken API client's request
signing/parsing, and the discovery pipeline's safety/tier logic against
synthetic and mocked data -- fast, deterministic, no network calls. They run
automatically on every push/PR via
[`.github/workflows/tests.yml`](.github/workflows/tests.yml). This is one
layer of a four-layer validation approach, each catching a different kind of
mistake before it costs real money:

1. **Unit tests** -- is the arithmetic right? (e.g. does a fee actually
   reduce the recorded return; does a flat/no-movement price series get
   read as neutral instead of "overbought" -- a real edge case these tests
   caught and fixed during development, see `backtest/strategies.py`'s `rsi()`)
2. **Backtests** (`--walk-forward`) -- does a strategy have real, out-of-sample edge?
3. **Paper trading** -- does the *whole pipeline* (discovery + regime +
   sizing + risk) behave sensibly against live data, with simulated fills?
4. **Live** -- real money, only after the first three build confidence, and
   only ever executed by a human, never by Claude.

## How it works

```
research/discover_candidates.py ──▶ Kraken AssetPairs + Ticker (find + liquidity/spread)
         │                       ──▶ CoinGecko (market cap, for tiering only)
         ▼
config/discovery.yaml (thresholds) ──┐
config/risk.yaml (position/exit rules)├──▶ .claude/skills/trade-cycle ──▶ kraken/propose_order.py (quote only)
state/circuit_breaker.json           │            │                              │
backtest/strategies.py ──────────────┘            ▼                    human runs --execute
                                       journal/trades.jsonl (audit log)      themselves
```

Each `trade-cycle` run: checks the kill switch, circuit breaker, and market
regime; runs (or reuses a fresh) live discovery pass to get this cycle's
safety-checked candidate list; generates signals from a regime-aware,
backtested strategy ensemble; sizes any resulting trade against hard caps
(volatility-scaled position size, portfolio heat, daily volume, slippage,
spread, concentration); **proposes and quotes** the trade (never executes
it -- see "Safety model"); and logs everything, including every rejected
candidate and why. See
[`.claude/skills/trade-cycle/SKILL.md`](.claude/skills/trade-cycle/SKILL.md)
for the full step-by-step and
[`docs/STRATEGY.md`](docs/STRATEGY.md) "Autonomous discovery" for the full
reasoning behind the safety pipeline.

## Safety model

- **Kill switch**: `config/risk.yaml`'s `enabled: false` stops all trading
  immediately.
- **Circuit breaker**: auto-trips and halts new trades if the portfolio
  drops to a configured floor or a large single-day drawdown.
- **Discovery + automated safety gate**: nothing is tradeable unless it's
  USD itself or passed `research/discover_candidates.py`'s live checks this
  cycle (24h volume, bid/ask spread, Kraken's own listing status) -- no
  human has to add a pair by name, and nothing gets a pass just because it
  looks promising. Lower-cap/lower-volume pairs are explicitly in scope,
  gated the same as anything else, sized smaller for their variance.
- **Market regime filter**: blocks *new* entries (never exits) while the
  broader market is trading below its own trend, so the agent isn't opening
  fresh risk into a market-wide downturn.
- **Volatility-scaled sizing + portfolio heat cap**: position size adjusts
  to each asset's own volatility, and total capital-at-risk across all open
  positions combined is capped independently of any single position's size.
- **Risk tiers**: every eligible pair is classified `blue_chip` /
  `established` / `emerging` from its own live data (market cap AND 24h
  volume), each with its own sizing multiplier -- smaller/thinner pairs get
  real but smaller exposure, not a blanket exclusion.
- **Concentration guardrails**: caps on emerging-tier positions,
  same-ecosystem positions, and non-stable exposure, so correlated pairs
  can't quietly become one oversized bet.
- **Position/volume/slippage caps**: enforced every cycle before any trade,
  independent of what the strategy signal says.
- **Human-only execution**: Claude proposes and quotes a trade
  (`kraken/propose_order.py`, no `--execute`) -- a human runs the actual
  `--execute` step themselves, every cycle, regardless of account size or
  how explicitly it's been asked to do otherwise. This is a hard boundary,
  not a configurable preference.
- **No withdrawal access**: the Kraken API key this project uses should
  never have Withdraw Funds permission enabled (see `docs/KRAKEN_SETUP.md`)
  -- a leaked/misused key can place trades, never move funds out.
- **Spot only**: margin and futures are explicitly out of scope
  (`config/risk.yaml`'s `asset_classes`) -- leverage/liquidation risk
  doesn't fit this project's "a loss floors out at 0%" risk model.

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for monitoring, pausing, and
circuit-breaker recovery.

## Repo layout

| Path | Purpose |
|---|---|
| `kraken/client.py` | Kraken REST API client (public data + authenticated Balance/AddOrder) |
| `kraken/propose_order.py` | Order proposal CLI -- quote-only by default, `--execute` places a real order (human-run only) |
| `config/risk.yaml` | Kill switch + all risk limits |
| `config/core_assets.yaml` | USD -- the one non-discovered settlement currency |
| `config/discovery.yaml` | Discovery sources, safety thresholds, tiers, pinned/denylist |
| `research/discover_candidates.py` | Live Kraken pair discovery + automated safety scoring |
| `.claude/skills/trade-cycle/` | The discover-decide-propose loop |
| `.claude/skills/backtest-strategy/` | Strategy validation workflow |
| `.claude/skills/paper-trade-cycle/` | Zero-risk simulated trading (full pipeline, no API key needed) |
| `backtest/` | Backtesting engine, strategies, single-pair CLI |
| `backtest/backtest_all.py` | Comprehensive backtest -- everything eligible + BTC/ETH, one ranked report |
| `paper_trading/run_paper_cycle.py` | Paper-trading simulator (real pipeline, simulated fills) |
| `tests/` | Unit tests (stdlib `unittest`) for strategies, engine, Kraken client, discovery, and paper-trading logic |
| `.github/workflows/tests.yml` | CI -- runs `tests/` on every push/PR |
| `docs/KRAKEN_SETUP.md` | API key setup & funding |
| `docs/STRATEGY.md` | Asset fundamentals, autonomous discovery, strategy/risk reasoning |
| `docs/RUNBOOK.md` | Day-to-day operation |
| `docs/TERMUX_SETUP.md` | Running on Android via Termux |
| `wallet/`, `docs/PHANTOM_MCP_SETUP.md`, `docs/LOCAL_WALLET_SETUP.md` | DEPRECATED -- the old Solana/Phantom pipeline, kept for historical reference |
| `journal/` | Append-only trade log (`trades.jsonl` real, `paper_trades.jsonl` simulated) |
| `state/circuit_breaker.json` | Circuit breaker status |
| `state/paper_portfolio.json` | Paper-trading simulated portfolio state |
