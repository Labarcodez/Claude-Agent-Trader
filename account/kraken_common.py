"""Shared Kraken-specific constants and helpers used across
research/discover_candidates.py and the rest of the account/ package --
single source of truth for asset-code normalization so the two can't
silently drift apart.
"""
from __future__ import annotations
import json
import re
import subprocess

# Kraken's legacy asset codes prefix fiat with "Z" and certain crypto (BTC,
# ETH, LTC, ...) with "X" for historical reasons (ISO-4217-style vs. the old
# pre-4217 convention Kraken originally launched with). altname on
# /AssetPairs and /Assets already strips this; this regex is the one place
# that logic lives for everything else that needs it (raw /Balance
# responses use the legacy codes directly, for instance).
LEGACY_ASSET_PREFIX = re.compile(r"^[XZ](?=[A-Z]{3,})")

# A few of Kraken's legacy codes don't just drop the X/Z prefix cleanly --
# XXBT strips to "XBT", which is Kraken's own longstanding code for what
# every other exchange/market-data source calls BTC. Applied after
# LEGACY_ASSET_PREFIX.sub(). Not claimed exhaustive -- extend if a live
# balance or pair ever surfaces another surprising code.
_ALTNAME_OVERRIDES = {"XBT": "BTC"}

# Settlement/cash-equivalent assets -- see config/core_assets.yaml. USD is
# fiat (exactly $1 by definition); USDT/USDC are stablecoins with their own
# small, real depeg risk -- usd_value_of_balances() defaults them to $1.00
# only when no real ticker price override is supplied.
STABLE_ASSETS = {"USD", "USDT", "USDC"}


def normalize_asset(code: str) -> str:
    """Normalizes a single Kraken asset code, e.g. 'XXBT' -> 'BTC',
    'ZUSD' -> 'USD', 'SOL' -> 'SOL' (no legacy prefix to strip). This is
    for a single asset code (as returned by /Balance), not a pair key like
    'XXBTZUSD' -- see research/discover_candidates.py for pair-altname
    handling, which is a separate concern."""
    if not code:
        return code
    stripped = LEGACY_ASSET_PREFIX.sub("", code)
    return _ALTNAME_OVERRIDES.get(stripped, stripped)


def run_kraken_cli(args: list[str], timeout: float = 30.0):
    """Best-effort convenience wrapper: shells out to the installed `kraken`
    CLI (kraken-cli) and parses its JSON output.

    This is NOT the primary integration path for the live trade-cycle
    skill -- that skill calls the Kraken MCP tools directly (already
    structured data, no subprocess involved) and should feed their result
    straight into this package's pure functions (normalize_balances,
    usd_value_of_balances, parse_fee_tier, etc.). This wrapper exists for
    manual/local testing convenience (e.g. `python3 -m account.portfolio`)
    and for any non-MCP automation that wants a plain Python call.

    Caveat, stated plainly: kraken-cli's exact JSON field names/nesting for
    a given subcommand were not verified against a live installed binary
    from this codebase's own development environment (no network access to
    do so at the time this was written). Spot-check `kraken <args> -o json`
    once against your own installation before trusting this wrapper's
    output shape blindly -- the functions downstream are written against
    Kraken's own documented REST API response shape (the "result" key
    unwrapped if present), which is the stable, authoritative source
    regardless of how the CLI happens to reshape it.
    """
    try:
        proc = subprocess.run(
            ["kraken", *args, "-o", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "`kraken` binary not found on PATH -- see docs/KRAKEN_CLI_SETUP.md to install kraken-cli."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"`kraken {' '.join(args)}` timed out after {timeout}s") from e

    stdout = (proc.stdout or "").strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"`kraken {' '.join(args)}` returned non-JSON output (exit {proc.returncode}): "
            f"{stdout[:500]!r} / stderr: {(proc.stderr or '')[:500]!r}"
        ) from e

    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"`kraken {' '.join(args)}` returned an error: {payload['error']}")
    if proc.returncode != 0:
        raise RuntimeError(f"`kraken {' '.join(args)}` exited {proc.returncode}: {(proc.stderr or stdout)[:500]}")
    return payload
