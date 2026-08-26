# Setting up Kraken

This project trades Kraken spot pairs via a REST API key. Unlike the old
Phantom MCP setup, there's no browser device-auth flow -- this works from
any machine, including a cloud/remote session, though placing a real order
is still always a deliberate, human-run action (see CLAUDE.md rule 1).

## 1. Create a Kraken account and fund it

Same philosophy as the rest of this project: fund it with only money you're
actually willing to risk. There's no fixed minimum -- `config/risk.yaml`'s
capital-derived caps (`max_position_usd_pct`, `circuit_breaker_floor_pct`,
etc.) scale to whatever the account actually holds.

## 2. Create an API key

In Kraken's web UI: **Settings -> API -> Add key**.

Scope the key's permissions as narrowly as Kraken allows:
- **Query Funds** -- required (balance checks)
- **Create & Modify Orders** -- required (placing orders)
- **Query Open/Closed Orders & Trades** -- useful, optional
- **Withdraw Funds -- NEVER enable this on this key.** There's no reason
  this project's automation needs withdrawal access, and enabling it turns
  a leaked key into a much bigger problem than a compromised trading key
  alone would be.

Kraken shows the **Private Key** (what this project calls
`KRAKEN_API_SECRET`) exactly once, at creation time. Copy it immediately --
if you lose it, you have to generate a new key pair, not just re-view it.

## 3. Fill in `.env`

Copy `.env.example` to `.env` (already gitignored -- never commit the real
one) if you haven't already, and fill in both values yourself in a local
text editor:

```
KRAKEN_API_KEY=<your API key>
KRAKEN_API_SECRET=<your Private Key, base64-encoded, exactly as Kraken showed it>
```

**Never paste these into chat with an AI agent, including this one.** Edit
the file directly.

Public data (`AssetPairs`, `Ticker`, `OHLC`) needs no key at all -- discovery
(`research/discover_candidates.py`), backtesting (`backtest/backtest_all.py`),
and paper trading (`paper_trading/run_paper_cycle.py`) all run fine with
`.env` left blank. The key is only needed for
[kraken/propose_order.py](../kraken/propose_order.py) `--execute` (placing a
real order) and for checking your real account balance.

## 4. Verify it works

```
python3 -c "from kraken.client import balance; print(balance())"
```
Should print your account's non-zero asset balances (an authenticated,
read-only call -- confirms the key/secret and signing are all correct
without touching the order book).

```
python3 kraken/propose_order.py XBTUSD buy 25.00
```
Quote-only (no `--execute`) -- prints an estimated fill and, if the key
above works, Kraken's own server-side order validation. Places no order,
moves no funds.

## Placing a real order

**This is always a human action, run by a human, in their own terminal --
never something an AI agent session does on your behalf, regardless of how
explicitly it's been asked to.** See CLAUDE.md rule 1 and
`.claude/skills/trade-cycle/SKILL.md` step 8.

```
python3 kraken/propose_order.py XBTUSD buy 25.00 --execute
```
