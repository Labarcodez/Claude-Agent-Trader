# Claude-Agent-Trader

An autonomous Solana trading agent, controlled through Claude Code + Phantom's
MCP server. It trades a small, deliberately-at-risk account (~$50) toward
growing that balance over time, using live token discovery, backtested
strategies, and hard risk limits. There is no hand-maintained token list --
the agent finds its own coins every cycle via `research/discover_candidates.py`
and only trades ones that pass automated, on-chain-backed safety checks.

## What this project is

- `.mcp.json` -- registers Phantom's official MCP server (`@phantom/mcp-server`).
- `config/risk.yaml` -- the risk/kill-switch config. **Read this before doing
  anything else with the wallet.**
- `config/core_assets.yaml` -- SOL/USDC, the only tokens exempt from live
  discovery (they're the settlement assets, not a trading candidate list).
- `config/discovery.yaml` -- discovery sources, automated safety thresholds,
  risk tiers, and the `pinned_candidates`/`denylist` manual overrides.
- `research/discover_candidates.py` -- pulls live candidates from Jupiter's
  Tokens API, filters them through on-chain safety checks, cross-checks
  survivors against RugCheck.xyz. This is what "the agent finds its own
  coins" means concretely -- read it before assuming a token needs to be
  added anywhere by hand.
- `.claude/skills/trade-cycle/` -- the autonomous discover-decide-execute loop.
- `.claude/skills/backtest-strategy/` -- validates strategies before they're
  trusted live.
- `.claude/skills/paper-trade-cycle/` -- runs the same pipeline against a
  simulated portfolio, no Phantom connection or real money required. Use
  this to validate changes and this session cannot run `trade-cycle` for
  real anyway (see rule 4).
- `backtest/` -- a dependency-free Python backtesting engine + strategies.
  `backtest_all.py` backtests everything currently discovery-eligible (plus
  SOL/BTC) against every strategy in one pass -- prefer it for a real
  cross-asset picture over one-off single-token runs.
- `paper_trading/run_paper_cycle.py` -- the paper-trading simulator.
- `tests/` -- unit tests (stdlib `unittest`) for the strategy math, engine
  mechanics, and discovery safety/tier logic. Run after touching
  `backtest/strategies.py`, `backtest/engine.py`, or
  `research/discover_candidates.py`: `python3 -m unittest discover -s tests -v`.
  Also runs in CI (`.github/workflows/tests.yml`) on every push/PR.
- `docs/STRATEGY.md` -- coin fundamentals, the autonomous discovery pipeline
  (with a real worked example), strategy and risk reasoning. Read this
  before changing discovery thresholds or strategy logic.
- `docs/PHANTOM_MCP_SETUP.md` -- how to connect and fund the wallet (must be
  done locally; Phantom's auth needs a local browser).
- `docs/RUNBOOK.md` -- day-to-day operation, paper trading, monitoring,
  stopping, and circuit-breaker recovery.
- `journal/` -- append-only log of every trading decision (git-ignored by
  default; it's live trading history, not source). `trades.jsonl` is real,
  `paper_trades.jsonl` is simulated -- never conflate the two.

## Ground rules for any agent (Claude) working in this repo

1. **`config/risk.yaml` is load-bearing, not a suggestion.** Never call a
   Phantom MCP write tool (`buy_token`, `transfer_tokens`,
   `send_solana_transaction`, `portfolio_rebalance`, etc.) without first
   checking `enabled: true` and `state/circuit_breaker.json`'s `tripped`
   status, per `.claude/skills/trade-cycle/SKILL.md`.
2. **Never trade a token that isn't SOL/USDC or wasn't `eligible: true` in
   the current cycle's `research/discover_candidates.py` output**, and never
   trade anything on `config/discovery.yaml`'s `denylist` regardless of what
   discovery says. Memecoins are explicitly in scope -- they go through the
   same automated gate, sized smaller via the `emerging` tier, not excluded.
   See `docs/STRATEGY.md`'s "Autonomous discovery".
3. **Never commit wallet secrets.** `~/.phantom-mcp/session.json` lives
   outside this repo and must stay there; `.gitignore` also blocks any
   `session.json` and the local `.phantom-mcp/` dir from being added here.
4. **This session (cloud/remote) cannot execute live trades.** Phantom's
   auth is a local browser flow. If asked to trade and no Phantom MCP tool
   is available, say so rather than fabricating wallet state -- see
   `docs/PHANTOM_MCP_SETUP.md`. `paper-trade-cycle` IS runnable here (no
   wallet needed) and is the right offer when someone wants to see the
   system trade without local setup.
5. **A strategy needs a non-negative, low-overfit-risk out-of-sample
   backtest on file before it trades live.** Run
   `.claude/skills/backtest-strategy` (with `--walk-forward`) after any
   change to `backtest/strategies.py` or before enabling a new one in
   `trade-cycle`. A specific token with no backtestable price history
   (common for freshly-discovered tokens) can still trade, but only at
   minimum size -- see `trade-cycle/SKILL.md` step 6.
6. **Log every cycle, including holds and rejected discovery candidates.**
   `journal/trades.jsonl` is the audit trail and debugging tool; a gap in it
   is a blind spot.
7. **Discovery thresholds are a safety boundary, not a suggestion to tune
   looser for more opportunities.** If `config/discovery.yaml`'s `safety`
   block changes, keep it in sync with the CLI defaults in
   `research/discover_candidates.py`, `paper_trading/run_paper_cycle.py`,
   and `backtest/backtest_all.py` (none of them parse the YAML -- see
   `research/discover_candidates.py`'s docstring for why), and understand
   why each threshold exists per `docs/STRATEGY.md` before loosening it.
8. **Run `python3 -m unittest discover -s tests -v` after touching any
   logic in `backtest/`, `research/discover_candidates.py`, or
   `paper_trading/run_paper_cycle.py`, before claiming the change works.**
   The test suite exists specifically because this kind of code has
   non-obvious edge cases (see `backtest/strategies.py`'s `rsi()` -- a
   flat/no-movement price series used to read as "overbought" until a test
   caught it); don't reintroduce what it's already checking for.
