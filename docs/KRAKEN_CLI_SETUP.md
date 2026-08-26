# Setting up Kraken CLI (the Kraken MCP server)

This connects Claude Code to your real Kraken account via
[`kraken-cli`](https://github.com/krakenfx/kraken-cli), Kraken's official,
open-source, AI-native CLI with a built-in MCP server. Unlike the project's
original Phantom/Solana setup, **authentication here is a plain API key +
secret, not a browser sign-in** -- there's no local-browser requirement in
principle. Whether it actually works in a given Claude Code session still
depends on that session's network egress policy allowing outbound HTTPS to
`api.kraken.com` (some cloud/remote environments restrict this; see the
Claude Code on the web docs on network policy) -- check that first if setup
seems to hang or every call errors out.

## 1. Install kraken-cli

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh
```

Supports macOS (Apple Silicon/Intel) and Linux (x86_64/ARM64, which covers
Termux on Android -- see `docs/TERMUX_SETUP.md`). Windows: use WSL. Verify:

```bash
kraken --version
kraken status   # public endpoint, no auth needed -- confirms network reachability
```

## 2. Create a Kraken API key

On [kraken.com](https://www.kraken.com), go to **Settings → API → Create
API Key**. Use the **principle of least privilege** -- create a key scoped
to only what the agent actually needs:

- **Required**: Query Funds, Query Open Orders & Trades, Query Closed
  Orders & Trades, Create & Modify Orders, Cancel/Close Orders.
- **Do NOT grant**: Withdraw Funds. There is no reason for a trading key to
  be able to move money off the exchange -- if you want the agent to be
  able to withdraw, use a *separate* key with that single extra permission,
  and grant it deliberately, not by default.
- Consider Kraken's optional key-level IP allowlist if the session's egress
  IP is stable enough to pin (it usually isn't for an ephemeral cloud
  session -- check before relying on this).

Save the API key and private key (secret) securely -- Kraken shows the
secret only once.

## 3. Configure credentials

**Environment variables (recommended -- what `.mcp.json` in this repo
assumes):**

```bash
export KRAKEN_API_KEY="your-key"
export KRAKEN_API_SECRET="your-secret"
```

Set these in your shell profile (persists across sessions) or, for a
Claude Code Remote / cloud session, as environment variables on the
session's environment configuration -- **never commit them to this repo.**
`.gitignore` already excludes common secret-file patterns, but env vars
aren't files at all, which is the point: nothing to accidentally `git add`.

Alternative: `kraken setup` writes an interactive config file to
`~/.config/kraken/config.toml` (created with `0600` permissions). Credential
resolution order is: CLI flags > environment variables > config file.

Verify:
```bash
kraken balance
```
This should return your account's real balances (likely all zero until you
fund it -- see step 4). An `auth` error here means the key/secret aren't
set or don't have the right permissions -- re-check step 2/3, not step 4.

## 4. `.mcp.json` is already in this repo

```json
{
  "mcpServers": {
    "kraken": {
      "command": "kraken",
      "args": ["mcp", "-s", "market,account,trade,paper,workspace,earn"]
    }
  }
}
```

Claude Code picks this up automatically for project-scoped MCP servers when
you open this repo, as long as `kraken` is on `PATH` and the environment
variables from step 3 are set for the process Claude Code runs in.

Notes on the service scopes chosen here:
- `market` -- public data, no auth, always safe.
- `account` -- read-only balance/order/trade history, needs auth.
- `trade` -- places/cancels real orders. **Dangerous** by kraken-cli's own
  classification -- MCP calls in this scope require `acknowledged=true` in
  guarded mode (the default; this repo does not pass `--allow-dangerous`,
  deliberately, so every live order needs an explicit acknowledgment rather
  than firing silently).
- `paper` / `workspace` -- Kraken CLI's own built-in paper trading, no real
  money, no auth needed.
- `earn` -- staking/yield allocation. Also dangerous-classified; same
  guarded-mode confirmation applies. Only ever used for **flexible**
  products per `config/risk.yaml`'s `flexible_only: true` -- see
  `docs/STRATEGY.md` "Idle-capital yield".
- Deliberately **not** included: `funding` (deposits/withdrawals) and
  `subaccount` -- this project's agent should never move funds off-exchange
  or between subaccounts on its own; do that manually, outside the skill.
- Deliberately **not** included: `futures` / `futures-paper` -- perpetual
  futures/leverage are out of scope by default (see `config/risk.yaml`'s
  `asset_classes`).

## 4b. Spot-check the `account/` package against your real installation

The `account/` package (`docs/STRATEGY.md` "Precise portfolio valuation &
fee-aware sizing") computes portfolio value and fee costs from the JSON the
Kraken MCP `balance`/`ticker`/`volume` tools return. Its parsing is written
against Kraken's own documented REST API response shapes, which is stable
and authoritative -- but if you ever use `account/portfolio.py`'s or
`account/fees.py`'s standalone CLI mode (which shells out to `kraken ... -o
json` instead of going through the MCP tools), run each once and compare:

```
kraken balance -o json
kraken volume --pair XBTUSD -o json
```

against what `account/kraken_common.run_kraken_cli`'s docstring expects
(the "result" key, if present, unwrapped; `fees`/`fees_maker` keyed by pair
for `volume`). If your installed kraken-cli version reshapes these
differently, adjust the lookups in `account/portfolio.py`'s `main()` /
`account/fees.py`'s `parse_fee_tier()` accordingly -- this was not verified
against a live installed binary when written (no network access to do so at
the time). This caveat does NOT apply to the live trade-cycle skill's
primary path (MCP tool JSON fed directly into the pure functions), only to
the CLI convenience wrappers.

## 5. Fund the account and verify before going live

1. Deposit whatever amount you actually intend to risk -- there's no
   required or minimum amount. Kraken supports fiat USD deposits (wire/ACH/
   SEPA, availability varies by jurisdiction) and crypto deposits; see
   Kraken's own funding docs for your region. The agent reads the account's
   actual balance on its first live cycle and sizes every risk cap off that
   (see `config/risk.yaml`'s "Capital" section).
2. Ask Claude to check the balance (`kraken balance` or the MCP tool) --
   should show your funded amount.
3. Run a backtest (`.claude/skills/backtest-strategy`) so there's at least
   one non-negative out-of-sample strategy result on file before the first
   live trade.
4. Confirm `config/risk.yaml` has `enabled: true` and the percentage-based
   caps reflect what you actually want (they scale automatically to
   whatever you funded the account with -- see that file's "Capital"
   section comments, no need to hand-tune them for a different account
   size).
5. Run one `trade-cycle` manually and read the output/journal entry before
   handing it to a loop/schedule -- see `docs/RUNBOOK.md`.

## Troubleshooting

- **"Kraken tool not found" / MCP server not connected**: confirm `kraken
  mcp -s market` starts standalone in a terminal without erroring, and that
  `kraken` is on `PATH` for the process Claude Code runs in.
- **`auth` errors on every account/trade call**: re-verify
  `KRAKEN_API_KEY`/`KRAKEN_API_SECRET` are set (case-sensitive) and the key
  has the permissions listed in step 2.
- **Every call times out or the gateway refuses the connection**: this
  session's network egress policy likely blocks `api.kraken.com` -- see the
  top of this doc and `.claude/skills/trade-cycle/SKILL.md` step 0. Test
  with a plain `kraken status` outside of Claude Code first to isolate
  whether it's a Claude Code/MCP issue or a network-policy one.
- **`rate_limit` errors**: read the returned `suggestion` field (kraken-cli
  surfaces Kraken's own tier-specific guidance rather than pre-throttling
  client-side) and reduce call frequency accordingly.

## References

- kraken-cli: https://github.com/krakenfx/kraken-cli
- Kraken API docs: https://docs.kraken.com/
- How to create an API key: https://support.kraken.com/articles/360000919966
