# Running this project in Termux (Android)

This project's Python side (discovery, backtesting, paper trading, tests) has
**zero third-party dependencies** -- stdlib only (`urllib`, `json`, `math`,
`statistics`) -- so it runs on Termux with nothing more than `python3`. This
has been verified live on-device. Live trading via Claude Code + Phantom MCP
also works, but needs one workaround for a known Android/Termux
incompatibility -- see Tier 2 below.

## Tier 1 -- discovery / backtesting / paper trading (no wallet needed)

```bash
# Install Termux from F-Droid, not the Play Store build (stale/unmaintained)
pkg update && pkg upgrade -y
pkg install -y python git

git clone https://github.com/Labarcodez/Claude-Agent-Trader.git
cd Claude-Agent-Trader
git checkout claude/phantom-mcps-trading-b9avtb

# Unit tests -- fast, no network
python3 -m unittest discover -s tests -v

# The real pipeline pieces
python3 research/discover_candidates.py       # live token discovery + safety checks
python3 backtest/backtest_all.py               # cross-asset backtest
python3 paper_trading/run_paper_cycle.py       # simulated trading cycle
```

Confirmed working end-to-end on-device: discovery found and safety-filtered
real candidates, `backtest_all.py` produced a full report, and
`run_paper_cycle.py` executed a minimum-size paper trade.

**Expected, non-error output you may see and can ignore:**
- `! could not fetch history: HTTP Error 404: Not Found` for a specific
  token during `backtest_all.py` -- means that particular contract isn't
  indexed on CoinGecko yet. The script skips it and keeps going; this is
  not a bug.
- `Rate limited, waiting 10s/20s/30s...` -- CoinGecko's free tier is
  strict; `backtest/fetch_history.py` already retries with backoff. The run
  still completes, just slower.

## Tier 2 -- live trading via Claude Code + Phantom MCP

This needs Node.js (for `npx`/the Claude Code CLI) and a browser sign-in per
[`PHANTOM_MCP_SETUP.md`](PHANTOM_MCP_SETUP.md). Termux has no GUI of its own,
but Android does have a browser -- the device-code URL Phantom prints can be
opened manually there.

### Known issue: Claude Code's native binary doesn't run on stock Termux

Since Claude Code v2.1.113, the CLI ships as a **native glibc-linked
binary**. Termux/Android uses **Bionic libc**, not glibc, so the binary
fails to execute even after a clean `npm install -g @anthropic-ai/claude-code`
(you'll likely see `npm warn install-scripts ... blocked` and then
`Error: claude native binary not installed`). `--allow-scripts` alone does
not fix this -- it lets the postinstall *run*, but the binary it downloads
still can't *execute* under Bionic. Tracked upstream:
[anthropics/claude-code#50270](https://github.com/anthropics/claude-code/issues/50270),
[#80574](https://github.com/anthropics/claude-code/issues/80574).

**Fix A -- proot-distro (recommended):** run a real Ubuntu userland inside
Termux via `proot`, which has genuine glibc:

```bash
pkg install -y proot-distro
proot-distro install ubuntu
proot-distro login ubuntu
# now inside the Ubuntu proot:
apt update && apt install -y nodejs npm git python3
cd ~/Claude-Agent-Trader   # your Termux home is visible at the same path from inside proot
npm install -g @anthropic-ai/claude-code
claude
```

**Fix B -- pin to the last pre-native-binary release** (simpler, but frozen
-- no updates, may lack newer skill/agent features):

```bash
npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code@2.1.112
```

Avoid third-party/unofficial npm wrapper packages that claim to "fix"
Claude Code on Termux -- this CLI will hold your Phantom wallet session
credentials once authenticated, so its install script isn't something to
hand to an unverified package.

### After `claude` runs

Start it from inside the repo directory so it picks up `.mcp.json`, then
follow [`PHANTOM_MCP_SETUP.md`](PHANTOM_MCP_SETUP.md) for the device-code
sign-in (open the printed URL in any Android browser) and wallet funding.

### Keeping long-running cycles alive

Android kills backgrounded Termux processes to save memory. For a `/loop`'d
`trade-cycle`, install `termux-api` (`pkg install termux-api` + the
Termux:API companion app from F-Droid) and hold `termux-wake-lock` for the
duration, or the OS may kill the session mid-cycle.
