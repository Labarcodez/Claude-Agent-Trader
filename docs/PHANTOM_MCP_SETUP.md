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
before it can do anything.** Send exactly what you intend to risk (per your
answer: $50 in SOL) from an exchange or your personal wallet to that address.
Double-check the address (paste it back, don't retype) before sending --
Solana transactions are irreversible.

Leave a small amount of extra SOL beyond the $50 trading capital for network
fees (a fraction of a cent per tx typically, but don't cut it to zero).

## 4. Verify before going live

1. Ask Claude to check the wallet balance (should show your funded SOL).
2. Run a backtest (`.claude/skills/backtest-strategy`) so there's at least
   one non-negative strategy result on file before the first live trade.
3. Confirm `config/risk.yaml` has `enabled: true` and the caps reflect what
   you actually want (defaults assume a $50 account -- see that file's
   comments).
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

## References

- Phantom MCP server docs: https://docs.phantom.com/phantom-mcp-server
- Package: https://www.npmjs.com/package/@phantom/mcp-server
