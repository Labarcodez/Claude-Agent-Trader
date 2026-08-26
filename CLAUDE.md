# Claude-Agent-Trader

An autonomous Kraken spot-trading agent, controlled through Claude Code. It
trades a small, deliberately-at-risk account -- fund it with whatever amount
you actually intend to risk, there is no fixed minimum -- toward growing
that balance over time, using live pair discovery, backtested strategies,
and hard risk limits scaled to whatever the account actually holds (see
`config/risk.yaml`'s "Capital" section). There is no hand-maintained pair
list -- the agent finds its own pairs every cycle via
`research/discover_candidates.py` and only trades ones Kraken lists as
tradable and that clear automated liquidity/spread checks.

(This project traded Solana meme/altcoins via Jupiter + Phantom MCP before
2026-08-26; that pipeline is retired -- see `wallet/DEPRECATED.md` and
`docs/PHANTOM_MCP_SETUP.md`/`docs/LOCAL_WALLET_SETUP.md` for what it was,
kept for historical reference only.)

## What this project is

- `kraken/` -- the Kraken REST API client (`client.py`, stdlib-only HMAC
  request signing), the order proposal/execution CLI (`propose_order.py`),
  order-precision/minimum-size safety (`precision.py`), and real-fee-tier-
  aware cost calculation (`fees.py`) -- see `docs/STRATEGY.md` "Fee-aware
  execution". **Read `config/risk.yaml` before doing anything else with the
  account.**
- `config/risk.yaml` -- the risk/kill-switch config.
- `config/core_assets.yaml` -- USD, the one thing exempt from live discovery
  (it's the settlement currency, not a trading candidate list).
- `config/discovery.yaml` -- discovery sources, automated safety thresholds,
  risk tiers, and the `pinned_candidates`/`denylist` manual overrides.
- `research/discover_candidates.py` -- pulls every online USD pair from
  Kraken's public AssetPairs/Ticker endpoints and filters them through
  liquidity/spread/tradability checks (Kraken already vets what it lists,
  so there's no on-chain audit or rug-check stage the way a DEX-listed token
  needed). This is what "the agent finds its own pairs" means concretely --
  read it before assuming a pair needs to be added anywhere by hand.
- `.claude/skills/trade-cycle/` -- the discover-decide-propose loop (Claude
  proposes and quotes a trade; a human runs the actual execute step).
- `.claude/skills/backtest-strategy/` -- validates strategies before they're
  trusted live.
- `.claude/skills/paper-trade-cycle/` -- runs the same pipeline against a
  simulated portfolio, no Kraken API key or real money required. Use
  this to validate changes, and this session cannot run `trade-cycle` for
  real execution anyway (see rule 4).
- `.claude/skills/review-trading-performance/` -- the feedback-loop skill:
  analyzes accumulated paper/live trading history
  (`scripts/analyze_journal.py`) for real, evidence-backed patterns, and
  turns a genuine one into a specific, human-reviewed proposed change. This
  is how the system improves over time -- see rule 10.
- `backtest/` -- a dependency-free Python backtesting engine + strategies.
  `backtest_all.py` backtests everything currently discovery-eligible (plus
  BTC/USD, ETH/USD) against every strategy in one pass -- prefer it for a
  real cross-asset picture over one-off single-pair runs. `engine.py` and
  `strategies.py` are venue-agnostic (operate on a generic cached price
  series) and were untouched by the Kraken migration.
- `paper_trading/run_paper_cycle.py` -- the paper-trading simulator.
- `tests/` -- unit tests (stdlib `unittest`) for the strategy math, engine
  mechanics, Kraken client signing/parsing, and discovery safety/tier logic.
  Run after touching `backtest/strategies.py`, `backtest/engine.py`,
  `kraken/client.py`, or `research/discover_candidates.py`:
  `python3 -m unittest discover -s tests -v`. Also runs in CI
  (`.github/workflows/tests.yml`) on every push/PR.
- `docs/STRATEGY.md` -- coin fundamentals, the autonomous discovery pipeline
  (with a real worked example), strategy and risk reasoning. Read this
  before changing discovery thresholds or strategy logic.
- `docs/KRAKEN_SETUP.md` -- how to create a scoped Kraken API key and fill
  in `.env`.
- `docs/RUNBOOK.md` -- day-to-day operation, paper trading, monitoring,
  stopping, and circuit-breaker recovery.
- `journal/` -- append-only log of every trading decision (git-ignored by
  default; it's live trading history, not source). `trades.jsonl` is real,
  `paper_trades.jsonl` is simulated -- never conflate the two.

## Ground rules for any agent (Claude) working in this repo

1. **`config/risk.yaml` is load-bearing, not a suggestion.** Never place a
   real Kraken order without first checking `enabled: true` and
   `state/circuit_breaker.json`'s `tripped` status, per
   `.claude/skills/trade-cycle/SKILL.md`. **In practice, Claude does not
   call the execute path at all** (`kraken/propose_order.py --execute`, or
   any other Kraken private order-placing endpoint) -- quoting and
   proposing a trade is as far as an agent session goes; a human runs the
   actual execution themselves, every cycle, regardless of account size or
   how the trade has been checked, and regardless of how explicitly this
   has been requested or authorized. This is a hard boundary, not a
   preference -- see trade-cycle SKILL.md step 8.
2. **Never trade a pair that isn't USD itself or wasn't `eligible: true` in
   the current cycle's `research/discover_candidates.py` output**, and never
   trade anything on `config/discovery.yaml`'s `denylist` regardless of what
   discovery says. Lower-cap/lower-volume pairs are explicitly in scope --
   they go through the same automated gate, sized smaller via the
   `emerging` tier, not excluded. See `docs/STRATEGY.md`'s "Autonomous
   discovery".
3. **Never commit account secrets.** `KRAKEN_API_KEY`/`KRAKEN_API_SECRET`
   live only in `.env` (gitignored) or the environment -- never in a config
   file, a commit, or chat. Scope the API key to Query Funds + Create/Modify
   Orders only; never enable Withdraw Funds on it (see
   `docs/KRAKEN_SETUP.md`).
4. **This session (cloud/remote) cannot execute live trades.** Even with a
   working `KRAKEN_API_KEY`, Claude never calls the execute path (rule 1).
   `paper-trade-cycle` IS runnable here (no key needed) and is the right
   offer when someone wants to see the system trade without local setup.
5. **A strategy needs a non-negative, low-overfit-risk out-of-sample
   backtest on file before it trades live.** Run
   `.claude/skills/backtest-strategy` (with `--walk-forward`) after any
   change to `backtest/strategies.py` or before enabling a new one in
   `trade-cycle`. A specific pair with little backtestable price history
   can still trade, but only at minimum size -- see `trade-cycle/SKILL.md`
   step 6.
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
   logic in `backtest/`, `research/discover_candidates.py`,
   `kraken/client.py`, or `paper_trading/run_paper_cycle.py`, before
   claiming the change works.** The test suite exists specifically because
   this kind of code has non-obvious edge cases (see `backtest/strategies.py`'s
   `rsi()` -- a flat/no-movement price series used to read as "overbought"
   until a test caught it); don't reintroduce what it's already checking for.
9. **Margin and futures are out of scope.** `config/risk.yaml`'s
   `asset_classes` only enables `kraken_spot` -- leverage means a position
   can lose more than it's sized at (liquidation), which breaks the
   assumption every cap in this project is built on (a spot loss floors out
   at 0%). Don't enable margin/futures as a config tweak; it's a separate,
   explicit decision the user hasn't made.
10. **"Learning from trading" means evidence-backed proposals, never a
    silent auto-tuner.** `.claude/skills/review-trading-performance`
    analyzes real closed-trade outcomes and proposes specific,
    human-reviewed changes -- it does not, and must never, edit
    `config/risk.yaml` or `config/discovery.yaml` on its own. A system that
    quietly retunes its own risk parameters from a small, noisy recent
    sample is a textbook way to overfit to noise, not genuine improvement.
