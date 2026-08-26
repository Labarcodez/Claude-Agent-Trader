> **DEPRECATED -- superseded by Kraken.** This project no longer trades via
> a local Solana keypair; see [KRAKEN_SETUP.md](KRAKEN_SETUP.md) for the
> current setup, and [../wallet/DEPRECATED.md](../wallet/DEPRECATED.md).
> Kept here for historical reference only.

# Local-keypair wallet (fallback for a broken Phantom MCP auth flow)

This is a **separate, alternate way to give the agent a wallet to trade
with** -- a plain local Solana keypair, signed with directly via
`@solana/web3.js` and Jupiter's public swap API, instead of Phantom MCP's
browser-based embedded wallet.

## Why this exists

Phantom MCP's device-code consent flow (`connect.phantom.app/device`) was
confirmed live on 2026-08-21 to have a permanently-disabled "Allow" button
that didn't respond to any local fix -- see `docs/PHANTOM_MCP_SETUP.md`'s
troubleshooting section for the full investigation (logout/re-login, a
fresh OAuth client registration, disabling browser extensions, switching to
Phantom's SSO flow, and checking Phantom's own status page all failed to
change the outcome). That points at a bug or outage in Phantom's own
hosted consent page/backend, not anything fixable from this repo. This
local-keypair path sidesteps it entirely by not depending on that page at
all.

**This is a different wallet identity than anything Phantom MCP creates.**
Phantom MCP's embedded wallet uses MPC/threshold signing (see the
`stamper`/KMS-based auth in `@phantom/cli`'s source) -- by design, there is
no single raw private key to export from a wallet created that way. So this
can never be the *same* wallet as an existing Phantom-MCP-created one
("Agent Wallet 1" if you have one already funded) -- it's a fresh,
separately-funded wallet. Whatever's in the Phantom-MCP wallet stays there,
recoverable once/if that flow works again or via Phantom support, entirely
independent of this one.

## Setup

1. `cd wallet && npm install` (already done if you're reading this after
   the initial build -- `wallet/node_modules/` is gitignored).
2. Copy `.env.example` to `.env` at the repo root and fill in
   `AGENT_WALLET_PRIVATE_KEY` yourself, in a local text editor -- base58
   string (Phantom's "Export Private Key" format) or a JSON array of 64
   bytes (`solana-keygen`'s format). **Never paste this into a chat with an
   AI agent** -- edit the file directly.
3. Get the wallet's public address (safe to view/share -- this command only
   ever prints the public key, never the private one):
   ```
   node wallet/address.mjs
   ```
4. Fund that address with **only what you're willing to lose** -- same
   "small, deliberately-at-risk account" philosophy as the rest of this
   project (see `CLAUDE.md`), just via a different signing mechanism. Leave
   a little extra SOL beyond your trading capital for network fees.
5. Confirm the funds landed:
   ```
   node wallet/balance.mjs
   ```
6. **Reset `state/starting_capital.json`** before running `trade-cycle`
   against this wallet -- that file holds the *previous* wallet's baseline
   (whatever Phantom MCP's wallet was funded with), and reusing it here
   would compute every risk cap (`max_position_usd`,
   `circuit_breaker_floor_usd`, etc.) off the wrong number. Delete the file
   (or move it aside) so the next cycle re-captures the baseline fresh from
   this wallet's actual funded balance, per
   `.claude/skills/trade-cycle/SKILL.md` step 2.

## What each script does

| Script | Reads funds? | Moves funds? |
| --- | --- | --- |
| `wallet/address.mjs` | No | No -- prints the public address only |
| `wallet/balance.mjs` | Yes (read-only RPC call) | No |
| `wallet/quote.mjs <in> <out> <amountBaseUnits> [slippageBps]` | No (public Jupiter quote, no key needed) | No |
| `wallet/swap.mjs <in> <out> <amountBaseUnits> [--slippage-bps N]` | No | No -- quote-only by default, same as Phantom's `buy_token(execute=false)` |
| `wallet/swap.mjs ... --execute` | Yes | **Yes -- signs and broadcasts a real transaction** |

Mint addresses: SOL is `So11111111111111111111111111111111111111112`,
USDC is `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (same constants as
`config/core_assets.yaml`). Amounts are in base units (lamports for SOL --
multiply SOL by 1e9; most SPL tokens use 6 decimals, check
`research/discover_candidates.py`'s discovery output or the token's mint
info if unsure).

## The one thing an AI agent should never do here

**Never let an AI agent run `wallet/swap.mjs ... --execute` (or any
equivalent that broadcasts a real transaction) on your behalf.** Building
the trade proposal, fetching quotes, checking risk limits -- all of that is
fine for the agent to do. The final `--execute` run, the one that actually
moves money, should be something a human runs deliberately in their own
terminal, every time, no matter how small the amount or how much the
change has already been explained and confirmed in chat.
