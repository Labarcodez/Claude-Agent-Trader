# Claude-Agent-Trader

An autonomous Solana trading agent, controlled through Claude Code + Phantom's
MCP server. It trades a small, deliberately-at-risk account (~$50) toward
growing that balance over time, using backtested strategies and hard risk
limits.

## What this project is

- `.mcp.json` -- registers Phantom's official MCP server (`@phantom/mcp-server`).
- `config/risk.yaml` -- the risk/kill-switch config. **Read this before doing
  anything else with the wallet.**
- `config/watchlist.yaml` -- the only tokens the agent may trade.
- `.claude/skills/trade-cycle/` -- the autonomous decide-and-execute loop.
- `.claude/skills/backtest-strategy/` -- validates strategies before they're
  trusted live.
- `backtest/` -- a dependency-free Python backtesting engine + strategies.
- `docs/STRATEGY.md` -- coin fundamentals, due-diligence checklist, strategy
  and risk reasoning. Read this before adding a token to the watchlist or
  changing strategy logic.
- `docs/PHANTOM_MCP_SETUP.md` -- how to connect and fund the wallet (must be
  done locally; Phantom's auth needs a local browser).
- `docs/RUNBOOK.md` -- day-to-day operation, monitoring, stopping, and
  circuit-breaker recovery.
- `journal/` -- append-only log of every trading decision (git-ignored by
  default; it's live trading history, not source).

## Ground rules for any agent (Claude) working in this repo

1. **`config/risk.yaml` is load-bearing, not a suggestion.** Never call a
   Phantom MCP write tool (`buy_token`, `transfer_tokens`,
   `send_solana_transaction`, `portfolio_rebalance`, etc.) without first
   checking `enabled: true` and `state/circuit_breaker.json`'s `tripped`
   status, per `.claude/skills/trade-cycle/SKILL.md`.
2. **Never trade a token that isn't on `config/watchlist.yaml` AND marked
   `verified: true` there.** New candidates -- including memecoins, which
   are explicitly in scope -- get proposed in the journal for a human to add
   and verify, not traded directly. See `docs/STRATEGY.md`'s due-diligence
   checklist.
3. **Never commit wallet secrets.** `~/.phantom-mcp/session.json` lives
   outside this repo and must stay there; `.gitignore` also blocks any
   `session.json` and the local `.phantom-mcp/` dir from being added here.
4. **This session (cloud/remote) cannot execute live trades.** Phantom's
   auth is a local browser flow. If asked to trade and no Phantom MCP tool
   is available, say so rather than fabricating wallet state -- see
   `docs/PHANTOM_MCP_SETUP.md`.
5. **A strategy needs a non-negative backtest on file before it trades live.**
   Run `.claude/skills/backtest-strategy` after any change to
   `backtest/strategies.py` or before enabling a new one in
   `trade-cycle`.
6. **Log every cycle, including holds.** `journal/trades.jsonl` is the audit
   trail and debugging tool; a gap in it is a blind spot.
7. **Mint addresses are high-stakes typos.** Verify any new token's mint
   address against an authoritative source before adding it to
   `config/watchlist.yaml` -- see `docs/STRATEGY.md`.
