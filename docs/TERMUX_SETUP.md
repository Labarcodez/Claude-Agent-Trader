# Running this project in Termux (Android)

This project's Python side (discovery, backtesting, paper trading, tests) has
**zero third-party dependencies** -- stdlib only (`urllib`, `json`, `math`,
`statistics`) -- so it runs on Termux with nothing more than `python3`. This
has been verified live on-device with the project's original Solana/Jupiter
pipeline; the Kraken-based pipeline uses the same stdlib-only approach
against a different (also public, key-free) REST API, so the same
should hold.

Live trading via Kraken CLI is a meaningfully easier story on Termux than
the project's original Phantom-based setup was: Kraken CLI ships a native
Linux ARM64 binary and authenticates with a plain API key + secret, not a
browser sign-in -- there's no device-code flow to relay through an Android
browser at all. See Tier 2 below.

## Tier 1 -- discovery / backtesting / paper trading (no account needed)

```bash
# Install Termux from F-Droid, not the Play Store build (stale/unmaintained)
pkg update && pkg upgrade -y
pkg install -y python git

git clone https://github.com/Labarcodez/Claude-Agent-Trader.git
cd Claude-Agent-Trader

# Unit tests -- fast, no network
python3 -m unittest discover -s tests -v

# The real pipeline pieces
python3 research/discover_candidates.py       # live Kraken pair discovery + liquidity/spread checks
python3 backtest/backtest_all.py               # cross-asset backtest
python3 paper_trading/run_paper_cycle.py       # simulated trading cycle
```

**Expected, non-error output you may see and can ignore:**
- `! could not fetch history: ...` for a specific pair during
  `backtest_all.py` -- Kraken's OHLC endpoint occasionally has gaps for
  very-recently-listed pairs. The script skips it and keeps going; this is
  not a bug.
- Occasional rate-limit backoff messages -- both Kraken's and CoinGecko's
  free public endpoints are rate-limited; the fetch helpers already retry
  with backoff. The run still completes, just slower.

## Tier 2 -- live trading via Claude Code + Kraken CLI

This needs Node.js (for `npx`/the Claude Code CLI) and kraken-cli itself.

### Known issue: Claude Code's native binary doesn't run on stock Termux

Since Claude Code v2.1.113, the CLI ships as a **native glibc-linked
binary**. Termux/Android uses **Bionic libc**, not glibc, so the binary
fails to execute even after a clean `npm install -g @anthropic-ai/claude-code`
(you'll likely see `npm warn install-scripts ... blocked` and then
`Error: claude native binary not installed`). `--allow-scripts` alone does
not fix this -- it lets the postinstall *run*, but the binary it downloads
still can't *execute* under Bionic. Tracked upstream:
[anthropics/claude-code#50270](https://github.com/anthropics/claude-code/issues/50270),
[#80574](https://github.com/anthropics/claude-code/issues/80574). This is a
Claude Code issue, not a Kraken CLI one -- it affects this project
regardless of which exchange/wallet integration is in use.

**Fix A -- proot-distro (recommended):** run a real Ubuntu userland inside
Termux via `proot`, which has genuine glibc:

```bash
pkg install -y proot-distro
proot-distro install ubuntu
proot-distro login ubuntu
# now inside the Ubuntu proot:
apt update && apt install -y nodejs npm git python3 curl
cd ~/Claude-Agent-Trader   # your Termux home is visible at the same path from inside proot
npm install -g @anthropic-ai/claude-code

# Install kraken-cli (Linux x86_64/ARM64 binaries -- works inside the proot)
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh

claude
```

**Fix B -- pin to the last pre-native-binary release** (simpler, but frozen
-- no updates, may lack newer skill/agent features):

```bash
npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code@2.1.112
```

Either way, install kraken-cli directly in Termux's own environment too
(its release binaries target Linux ARM64 natively, no proot required for
the CLI itself -- only Claude Code's own native binary needs the glibc
workaround above):

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh
kraken --version
```

Avoid third-party/unofficial npm wrapper packages that claim to "fix"
Claude Code on Termux -- once configured, this CLI has access to your
Kraken API credentials (via environment variables, not stored session
files -- see `docs/KRAKEN_CLI_SETUP.md`), so its install script isn't
something to hand to an unverified package.

### After `claude` runs

Start it from inside the repo directory so it picks up `.mcp.json`, then
follow `docs/KRAKEN_CLI_SETUP.md` for API key creation and setting
`KRAKEN_API_KEY`/`KRAKEN_API_SECRET` as environment variables (`export` in
`~/.bashrc` or Termux's own shell profile persists them across sessions --
no browser step needed at all, unlike the project's original Phantom setup).

### Keeping long-running cycles alive

Android kills backgrounded Termux processes to save memory. For a `/loop`'d
`trade-cycle`, install `termux-api` (`pkg install termux-api` + the
Termux:API companion app from F-Droid) and hold `termux-wake-lock` for the
duration, or the OS may kill the session mid-cycle.
