# Running this project in Termux (Android)

This project's Python side (discovery, backtesting, paper trading, live
Kraken execution, tests) has **zero third-party dependencies** -- stdlib
only (`urllib`, `json`, `hmac`, `hashlib`, `base64`) -- so it runs on Termux
with nothing more than `python3`. Unlike the old Solana pipeline, there's no
Node.js dependency and no browser device-auth flow needed anywhere in this
project anymore -- placing a real Kraken order (`kraken/propose_order.py
--execute`) works from the same plain `python3` environment as everything
else below.

## Discovery / backtesting / paper trading / live execution

```bash
# Install Termux from F-Droid, not the Play Store build (stale/unmaintained)
pkg update && pkg upgrade -y
pkg install -y python git

git clone https://github.com/Labarcodez/Claude-Agent-Trader.git
cd Claude-Agent-Trader

# Unit tests -- fast, no network
python3 -m unittest discover -s tests -v

# The real pipeline pieces -- none of these need a Kraken API key
python3 research/discover_candidates.py       # live Kraken pair discovery + safety checks
python3 backtest/backtest_all.py               # cross-asset backtest
python3 paper_trading/run_paper_cycle.py       # simulated trading cycle

# Fill in .env (see docs/KRAKEN_SETUP.md) to check a real balance or place a real order
python3 -c "from kraken.client import balance; print(balance())"
python3 kraken/propose_order.py XBTUSD buy 25.00              # quote only
python3 kraken/propose_order.py XBTUSD buy 25.00 --execute    # places a real order -- run this deliberately, yourself
```

**Expected, non-error output you may see and can ignore:**
- Occasional `Rate limited, waiting Xs...` / `Connection error, waiting
  Xs...` from `backtest/fetch_history.py`'s CoinGecko path (used only for
  market-cap lookups, not price history) -- CoinGecko's free tier is
  strict; the retry-with-backoff is expected behavior, not a bug. The run
  still completes, just slower.

## Running Claude Code itself on Termux

If you also want to run Claude Code's interactive CLI on-device (rather
than just this project's plain Python scripts above), there's a known,
unrelated Android/Termux compatibility issue worth knowing about upfront:

### Claude Code's native binary doesn't run on stock Termux

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
Claude Code on Termux -- this CLI will have access to your `.env` file
(Kraken API credentials) once configured, so its install script isn't
something to hand to an unverified package.

### Keeping long-running cycles alive

Android kills backgrounded Termux processes to save memory. For a `/loop`'d
`trade-cycle`, install `termux-api` (`pkg install termux-api` + the
Termux:API companion app from F-Droid) and hold `termux-wake-lock` for the
duration, or the OS may kill the session mid-cycle.
