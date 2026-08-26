> **DEPRECATED -- superseded by Kraken.** This project no longer trades via
> Phantom MCP or Solana; see [KRAKEN_SETUP.md](KRAKEN_SETUP.md) for the
> current setup. Kept here for historical reference only -- nothing in
> `.claude/skills/`, `config/`, or the active pipeline references this
> anymore.

# Setting up the Phantom MCP server

This connects Claude Code to a real Solana wallet Phantom creates and manages
for the agent. **This must be done on a machine where you can complete a
browser sign-in** -- your laptop/desktop running Claude Code or Claude
Desktop locally. It will not work inside an ephemeral cloud/remote session
(no local browser to complete the auth handshake).

## 1. Config is already in this repo

`.mcp.json` at the repo root already declares the server:

```json
{
  "mcpServers": {
    "phantom": {
      "command": "npx",
      "args": ["-y", "@phantom/mcp-server@latest"]
    }
  }
}
```

Claude Code picks this up automatically for project-scoped MCP servers when
you open this repo. You need Node.js/npm installed locally (npx ships with
npm).

## 2. Authenticate

Start Claude Code in this repo directory and ask it to list available MCP
tools, or just start the `trade-cycle` skill -- the first Phantom tool call
will trigger a **device-code sign-in**: a browser window opens, you approve
the connection with your Phantom account.

Important: **this creates a new, dedicated wallet for the agent** -- it is
*not* your existing personal Phantom wallet. That's deliberate: it keeps the
agent's blast radius limited to whatever you fund it with, separate from any
other holdings.

Session credentials (wallet ID, org ID, stamper keys) are cached at
`~/.phantom-mcp/session.json` on your machine after first auth, so you don't
have to re-authenticate every session. This file is sensitive -- it's what
lets the agent sign transactions. Never commit it or share it (it's already
excluded via `.gitignore`).

## 3. Find the wallet address and fund it

Ask Claude to call the Phantom `get_wallet_addresses` tool to get the
agent's Solana address. The wallet starts empty -- **you must send it SOL
before it can do anything.** Send whatever amount you intend to risk in SOL
from an exchange or your personal wallet to that address -- there's no
required or minimum amount; the agent reads the wallet's actual balance on
its first live cycle and sizes every risk cap off that (see
`config/risk.yaml`'s "Capital" section). Double-check the address (paste it
back, don't retype) before sending -- Solana transactions are irreversible.

Leave a small amount of extra SOL beyond your intended trading capital for
network fees (a fraction of a cent per tx typically, but don't cut it to
zero).

## 4. Verify before going live

1. Ask Claude to check the wallet balance (should show your funded SOL).
2. Run a backtest (`.claude/skills/backtest-strategy`) so there's at least
   one non-negative strategy result on file before the first live trade.
3. Confirm `config/risk.yaml` has `enabled: true` and the percentage-based
   caps reflect what you actually want (they scale automatically to
   whatever you funded the wallet with -- see that file's "Capital" section
   comments, no need to hand-tune them for a different account size).
4. Run one `trade-cycle` manually and read the output/journal entry before
   handing it to a loop/schedule -- see `docs/RUNBOOK.md`.

## Troubleshooting

- **"Phantom tool not found" / MCP server not connected**: confirm `npx -y
  @phantom/mcp-server@latest` runs standalone in a terminal without erroring;
  confirm you're running Claude Code locally (not a cloud/remote session)
  since the browser auth step needs a local browser.
- **Insufficient balance errors**: check you funded the *agent's* wallet
  address (from `get_wallet_addresses`), not your personal Phantom wallet.
- **Re-authenticating on a new machine**: delete/ignore the old
  `~/.phantom-mcp/session.json` and repeat step 2; it'll issue a fresh
  session (same underlying wallet, tied to your Phantom account).
- **Consent screen's "Allow" button is permanently disabled / unclickable
  (device-connect flow stuck)**: confirmed live 2026-08-21 against an
  already-funded wallet ("Agent Wallet 1", ~$56). Full investigation and
  outcome, so a future session doesn't repeat it:
  - `phantom_logout` clears `session.json` but does **not** clear
    `~/.phantom-mcp/agent-registration.json` -- the cached OAuth Dynamic
    Client Registration (DCR) `client_id` the device-code flow
    (`DeviceCodeAuthProvider` in `@phantom/cli`, RFC 8628 device-code against
    `connect.phantom.app/device-connect`) reuses on every login so it keeps
    resolving to the *same* wallet across sessions.
  - Moving that file aside to force a brand-new DCR registration did **not**
    fix the disabled button -- it was disabled again immediately on the
    fresh registration too. That rules out "this one specific registration
    is stuck" as the cause; it points at an instability in the device-code
    consent page itself, unrelated to which client is used.
  - **Do not leave a regenerated/fresh DCR registration in place, and do not
    switch to the SSO flow, as a workaround.** Both mint a brand-new
    `client_id`, and wallet identity on Phantom's backend is resolved
    partly from that `client_id` (`_getOrCreateAppWallet` in
    `DeviceCodeAuthProvider.ts`) -- completing auth on a *different*
    `client_id` than the one that originally created your funded wallet
    risks the flow creating and attaching a second, empty wallet instead of
    reconnecting to the one holding real funds. (The SSO flow is *worse* on
    this axis, not better: `OAuthFlow.authenticate()` in `@phantom/cli`
    doesn't cache to `agent-registration.json` at all -- it registers a
    fresh, never-reused client on literally every login attempt, and its
    own log line says outright: `"DCR is not currently supported by
    auth.phantom.app - you should provide PHANTOM_APP_ID or
    PHANTOM_CLIENT_ID"`.) If you ever need to force a fresh DCR
    registration to test something, move the old
    `agent-registration.json` aside rather than deleting it, and restore it
    afterward rather than trading with whatever new wallet a fresh
    registration might attach to.
  - Net conclusion: with the original `agent-registration.json` restored
    (the safe, wallet-identity-preserving state), the disabled-button
    symptom itself remains unexplained from anything in this repo, your
    local machine, or even this npm package's client-side code -- it did
    not respond to logout, a fresh registration, disabling browser
    extensions, or a different auth flow entirely. That combination points
    at a genuine bug/outage in Phantom's own hosted consent page or backend,
    not something fixable locally. If it recurs: check
    https://github.com/orgs/phantom/discussions and
    https://help.phantom.com for open reports, or contact Phantom support,
    rather than spending more time on local workarounds -- and don't
    experiment with anything that mints a new OAuth client (DCR reset, SSO
    flow, `PHANTOM_CLIENT_ID` override to an unfamiliar value) against a
    wallet that already holds funds without first confirming, from
    Phantom's side, that doing so won't attach a different wallet.
  - If you don't want to wait on Phantom, `docs/LOCAL_WALLET_SETUP.md`
    documents a separate local-keypair execution path (plain Solana
    keypair + `@solana/web3.js` + Jupiter's swap API, no browser consent
    screen involved) that sidesteps this entirely. It's a genuinely
    different wallet, funded fresh -- not a way to unstick funds already in
    a Phantom-MCP-created wallet.

## References

- Phantom MCP server docs: https://docs.phantom.com/phantom-mcp-server
- Package: https://www.npmjs.com/package/@phantom/mcp-server
