# DEPRECATED -- superseded by Kraken

This directory (a local Solana keypair + Jupiter-swap fallback for when
Phantom MCP's browser auth was broken/unavailable) is no longer used by this
project. Trading moved to Kraken -- see [docs/KRAKEN_SETUP.md](../docs/KRAKEN_SETUP.md)
and [kraken/](../kraken/) for the replacement.

The `.mjs` files here are left on disk, untouched, for historical reference
(git history has the full story either way) -- nothing in `.claude/skills/`,
`config/`, or the active pipeline (`research/`, `backtest/`, `paper_trading/`)
references this directory anymore. If you were using the wallet this held,
its keypair is unaffected by any of this -- see the note in `.env` about
where its private key was backed up.

Deleting this directory entirely is a reasonable follow-up if you're sure
you don't need it, but it hasn't been done automatically here.
