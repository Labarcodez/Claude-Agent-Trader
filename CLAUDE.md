# Claude-Agent-Trader

An autonomous crypto trading agent for the **Kraken exchange**, controlled
through Claude Code + [Kraken CLI](https://github.com/krakenfx/kraken-cli)'s
built-in MCP server so Claude can research, discover, decide, size, and
execute trades directly against a real Kraken account -- with live pair
discovery, backtesting, and hard risk limits built in. This project
originally ran on Solana via Phantom's MCP server; it has been fully
migrated to Kraken (see "Why Kraken, not Phantom" below) -- there is no
Solana/Phantom code left in this repo, and none should be reintroduced
without a deliberate decision to do so.

It trades a small, deliberately-at-risk account -- fund it with whatever
amount you actually intend to risk, there is no fixed minimum -- toward
growing that balance over time, using live pair discovery, backtested
strategies, and hard risk limits scaled to whatever the account actually
holds (see `config/risk.yaml`'s "Capital" section). There is no
hand-maintained pair list -- the agent finds its own tradeable pairs every
cycle via `research/discover_candidates.py`, scored against Kraken's own
live liquidity and spread data, and only trades ones that pass those checks.

## Why Kraken, not Phantom

Phantom's MCP server authenticates via a **local browser sign-in** -- it
fundamentally cannot complete in an ephemeral cloud/remote Claude Code
session with no browser, which made autonomous cloud-hosted trading
impossible on that stack no matter what was tried. Kraken's API
authenticates with a plain **API key + secret** (HMAC-signed requests) --
there is no browser step at all, in principle. Whether a given session can
actually reach `api.kraken.com` still depends on that session's network
egress policy (see the Claude Code on the web docs) -- as of this
migration, this project's own default cloud session type blocks it (see
rule 4 below) -- but that's a configurable network-policy question, not a
structural dead end the way Phantom's browser requirement was. Kraken also
adds things Phantom/Jupiter had no equivalent of at all: a disclosed
maker/taker fee schedule worth optimizing against, and a native yield
product (Earn) for idle cash between trades -- see `docs/STRATEGY.md`.

## What this project is

- `.mcp.json` -- registers Kraken CLI's built-in MCP server (`kraken mcp`).
- `config/risk.yaml` -- the risk/kill-switch config. **Read this before doing
  anything else with the account.**
- `config/core_assets.yaml` -- USD/USDT, the only settlement currencies
  exempt from live discovery (they're cash, not a trading candidate).
- `config/discovery.yaml` -- discovery sources, automated liquidity/spread
  thresholds, risk tiers, and the `pinned_candidates`/`denylist` manual
  overrides.
- `research/discover_candidates.py` -- pulls every tradable pair from
  Kraken's public AssetPairs/Ticker endpoints, filters by live 24h volume
  and bid/ask spread. This is what "the agent finds its own pairs" means
  concretely -- read it before assuming a pair needs to be added anywhere
  by hand.
- `account/` -- deterministic portfolio valuation (`portfolio.py`), fee
  calculation (`fees.py`), and order-precision/minimum-size safety
  (`precision.py`). The trade-cycle skill calls these instead of computing
  "how much is in the account" or "what will this cost in fees" by hand --
  see `docs/STRATEGY.md` "Precise portfolio valuation & fee-aware sizing".
- `.claude/skills/trade-cycle/` -- the autonomous discover-decide-execute loop.
- `.claude/skills/backtest-strategy/` -- validates strategies before they're
  trusted live.
- `.claude/skills/paper-trade-cycle/` -- runs the same pipeline against a
  simulated portfolio, no Kraken account or API key required. Use this to
  validate changes when this session cannot run `trade-cycle` for real
  anyway (see rule 4).
- `backtest/` -- a dependency-free Python backtesting engine + strategies.
  `backtest_all.py` backtests everything currently discovery-eligible (plus
  BTC/ETH) against every strategy in one pass -- prefer it for a real
  cross-asset picture over one-off single-pair runs.
- `paper_trading/run_paper_cycle.py` -- the paper-trading simulator.
- `tests/` -- unit tests (stdlib `unittest`) for the strategy math, engine
  mechanics, and discovery liquidity/tier logic. Run after touching
  `backtest/strategies.py`, `backtest/engine.py`, or
  `research/discover_candidates.py`: `python3 -m unittest discover -s tests -v`.
  Also runs in CI (`.github/workflows/tests.yml`) on every push/PR.
- `docs/STRATEGY.md` -- how to actually make money on Kraken: fee-aware
  execution, idle-capital yield via Earn, the autonomous discovery pipeline,
  strategy and risk reasoning. Read this before changing discovery
  thresholds or strategy logic.
- `docs/KRAKEN_CLI_SETUP.md` -- how to install Kraken CLI, create a
  least-privilege API key, and fund the account.
- `docs/RUNBOOK.md` -- day-to-day operation, paper trading, monitoring,
  stopping, and circuit-breaker recovery.
- `docs/TERMUX_SETUP.md` -- running on Android via Termux.
- `journal/` -- append-only log of every trading decision (git-ignored by
  default; it's live trading history, not source). `trades.jsonl` is real,
  `paper_trades.jsonl` is simulated -- never conflate the two.

## Ground rules for any agent (Claude) working in this repo

1. **`config/risk.yaml` is load-bearing, not a suggestion.** Never call a
   dangerous Kraken MCP tool (`order buy`/`order sell`/`order cancel-all`,
   `earn allocate`/`deallocate`, any `funding`/`subaccount` tool) without
   first checking `enabled: true` and `state/circuit_breaker.json`'s
   `tripped` status, per `.claude/skills/trade-cycle/SKILL.md`. Also check
   `config/risk.yaml`'s `asset_classes` before any order -- only
   `kraken_spot` is enabled by default; never place a margin or futures
   order (leverage) even if the MCP server exposes the tool for it, unless
   a human has explicitly turned that asset class on.
2. **Never trade a pair that isn't a settlement currency (USD/USDT, see
   `config/core_assets.yaml`) or wasn't `eligible: true` in the current
   cycle's `research/discover_candidates.py` output**, and never trade
   anything on `config/discovery.yaml`'s `denylist` regardless of what
   discovery says. Smaller/emerging-tier pairs are explicitly in scope --
   they go through the same automated gate, sized smaller via the
   `emerging` tier, not excluded. See `docs/STRATEGY.md`'s "Autonomous
   discovery".
3. **Never commit Kraken API credentials.** `KRAKEN_API_KEY`/
   `KRAKEN_API_SECRET` belong in environment variables; kraken-cli's own
   config file lives at `~/.config/kraken/config.toml`, outside this repo.
   `.gitignore` also blocks common credential-file patterns from being
   added here as a backstop.
4. **This session (cloud/remote) currently cannot execute live trades.**
   Unlike Phantom's browser-auth requirement, this is a network-policy
   limitation, not a structural one -- Kraken's API auth (key + secret)
   needs no browser in principle. But this project's own default cloud
   session type has been confirmed (during this migration) to block
   outbound HTTPS to `api.kraken.com` at the network egress layer, which
   means neither live trading nor even the key-free public-data calls
   (`research/discover_candidates.py`, `paper_trading/run_paper_cycle.py`)
   can reach Kraken from a session configured that way. If asked to trade
   and Kraken MCP tools error out or aren't available, check
   `.claude/skills/trade-cycle/SKILL.md` step 0 to tell which of "not
   configured" vs. "network policy blocks it" is the actual cause, and say
   so rather than fabricating account state -- see
   `docs/KRAKEN_CLI_SETUP.md`. A differently-configured environment (or a
   local machine) is not subject to this the same way Phantom's browser
   requirement categorically was.
5. **A strategy needs a non-negative, low-overfit-risk out-of-sample
   backtest on file before it trades live.** Run
   `.claude/skills/backtest-strategy` (with `--walk-forward`) after any
   change to `backtest/strategies.py` or before enabling a new one in
   `trade-cycle`. A specific pair with no backtestable price history
   (possible for a freshly-eligible pair) can still trade, but only at
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
   logic in `backtest/`, `research/discover_candidates.py`,
   `paper_trading/run_paper_cycle.py`, or `account/`, before claiming the
   change works.** The test suite exists specifically because this kind of
   code has non-obvious edge cases (see `backtest/strategies.py`'s `rsi()`
   -- a flat/no-movement price series used to read as "overbought" until a
   test caught it); don't reintroduce what it's already checking for.
9. **Never enable margin, futures, or Bonded Earn without a deliberate,
   explicit human decision.** `config/risk.yaml`'s `asset_classes` and
   `kraken_earn.flexible_only: true` encode this; see `docs/STRATEGY.md`
   "What this project deliberately doesn't do (yet)" and "Idle-capital
   yield" for why -- leverage and locked capital both silently invalidate
   this project's risk model if turned on casually.
10. **Never hand-compute portfolio value, fee cost, or order price/volume
    precision when the `account/` package can do it deterministically.**
    Feed the Kraken MCP tools' JSON straight into `account/portfolio.py`'s
    `usd_value_of_balances()`, `account/fees.py`'s `parse_fee_tier()`/
    `edge_clears_costs()`, and `account/precision.py`'s `round_price()`/
    `round_volume()`/`clamp_to_pair_minimums()` per
    `.claude/skills/trade-cycle/SKILL.md` steps 2, 7, and 8 -- these
    functions exist specifically because this arithmetic is easy to get
    subtly wrong by hand, and every risk check in this project depends on
    it being right.
