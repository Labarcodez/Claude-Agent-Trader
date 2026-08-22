#!/usr/bin/env python3
"""Fetch historical daily price/volume history from CoinGecko's free public API
and cache it locally as JSON, for use by backtest/engine.py.

No API key required (uses the public api.coingecko.com endpoints, which are
rate-limited but sufficient for periodic backtesting). Stdlib only.

Two lookup modes:
  --coin <coingecko-id>              e.g. solana, jupiter-exchange-solana
  --contract <mint> [--platform solana]   any Solana token by mint address --
                                           this works even for tokens with no
                                           CoinGecko "coin id" of their own
                                           (verified live against a same-day
                                           pump.fun launch), which is what
                                           makes freshly-discovered tokens
                                           (see research/discover_candidates.py)
                                           backtestable at all.

Usage:
    python3 backtest/fetch_history.py --coin solana --days 180
    python3 backtest/fetch_history.py --contract JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN --days 30
"""
from __future__ import annotations
import argparse
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"
BASE_URL = "https://api.coingecko.com/api/v3"


def _resample_to_daily(series: list[list]) -> list[list]:
    """Collapses a CoinGecko [timestamp_ms, value] series down to one point
    per UTC calendar day (the last observation of that day = the day's
    close), regardless of the source granularity.

    Why this exists: CoinGecko's free market_chart endpoint auto-selects
    granularity by range -- hourly for any days<=90 request, daily only
    above 90 (verified live: days=90 returns 2,161 hourly points; days=91
    returns 92 daily points). Every "daily-scale" indicator in this repo
    (SMA10/30, RSI14, the regime filter's N-day SMA, volatility_breakout's
    20-bar lookback) assumes each series entry is one day. Left unresampled,
    a days<=90 request (backtest_all.py's default, paper trading's signal
    history, and the regime filter's SMA window all request <=90) silently
    turns a "30-day SMA" into a ~30-*hour* SMA -- wrong by roughly 24x,
    without erroring or looking obviously broken. Resampling once here, at
    the source, means every consumer (engine, paper trading, regime filter)
    gets true daily bars no matter what granularity the API happened to
    return, instead of each caller needing to know and guard against
    CoinGecko's range-dependent behavior itself."""
    if not series:
        return series
    by_day: dict[str, list] = {}
    for point in series:
        day_key = datetime.fromtimestamp(point[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day_key] = point  # last observation of the day wins; dict preserves first-seen (chronological) key order
    return list(by_day.values())


# 429 (rate limited) and 502/503/504 (bad gateway/unavailable/gateway
# timeout) are all transient, worth retrying -- unlike a definitive client
# error, which retrying can't fix. Mirrors RETRYABLE_HTTP_CODES in
# research/discover_candidates.py's _get_json() (added after RugCheck.xyz
# returned 502 for otherwise-clean candidates live) -- this file talks to a
# different upstream (CoinGecko) but is exactly as exposed to the same
# transient-error class, and previously only retried 429.
RETRYABLE_HTTP_CODES = {429, 502, 503, 504}


def _fetch(url: str, retries: int = 4, base_wait: float = 10.0) -> dict:
    """retries/base_wait are overridable because this function's callers have
    very different patience budgets: a one-off backtest run can afford the
    default (up to 4 attempts, 10/20/30/40s backoff, ~100s worst case), but
    paper_trading/run_paper_cycle.py's opportunistic per-candidate signal
    fetches run inside a tight 15-minute cron loop, where even a few
    candidates each hitting worst-case backoff can push a single cycle past
    a minute or two -- observed live, repeatedly. A candidate paper trading
    gives up on quickly just gets reconsidered next cycle (discovery
    rotation persists), so failing fast there costs nothing but a delay."""
    req = urllib.request.Request(url, headers={"User-Agent": "claude-agent-trader/1.0"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in RETRYABLE_HTTP_CODES:
                wait = base_wait * (attempt + 1)
                print(f"Rate limited, waiting {wait:.0f}s...")
                time.sleep(wait)
                continue
            raise
        except OSError as e:
            # This function previously retried nothing but HTTPError, so any
            # connection-level failure (timeout, reset, DNS hiccup) crashed
            # the whole run on the first occurrence -- confirmed live via an
            # uncaught http.client.RemoteDisconnected from a different
            # upstream (RugCheck.xyz, in research/discover_candidates.py's
            # _get_json, which had the same gap -- see that fix for why
            # urllib doesn't always wrap these as URLError). Mirroring the
            # same broad-OSError retry here for the same reason.
            last_err = e
            wait = base_wait * (attempt + 1)
            print(f"Connection error, waiting {wait:.0f}s...")
            time.sleep(wait)
    raise last_err


def fetch_market_chart(coin_id: str, days: int, vs_currency: str = "usd",
                        retries: int = 4, base_wait: float = 10.0) -> dict:
    """Returns CoinGecko's market_chart payload: {prices, market_caps, total_volumes},
    each a list of [unix_ms, value] pairs, looked up by CoinGecko coin id.
    Resampled to one point per UTC day regardless of what granularity the API
    returned -- see _resample_to_daily(). retries/base_wait: see _fetch()."""
    payload = _fetch(f"{BASE_URL}/coins/{coin_id}/market_chart?vs_currency={vs_currency}&days={days}",
                      retries=retries, base_wait=base_wait)
    return {k: (_resample_to_daily(v) if k in ("prices", "market_caps", "total_volumes") else v)
            for k, v in payload.items()}


def fetch_market_chart_by_contract(contract: str, days: int, platform: str = "solana",
                                    vs_currency: str = "usd", retries: int = 4, base_wait: float = 10.0) -> dict:
    """Same payload shape as fetch_market_chart, looked up by token contract/mint
    address instead of a CoinGecko coin id. Works for tokens that were never
    given a curated CoinGecko listing (confirmed live against a same-day
    pump.fun launch) -- this is what makes freshly-discovered tokens
    backtestable without waiting for CoinGecko to index them by name.
    Resampled to one point per UTC day -- see _resample_to_daily().
    retries/base_wait: see _fetch()."""
    payload = _fetch(f"{BASE_URL}/coins/{platform}/contract/{contract}/market_chart?vs_currency={vs_currency}&days={days}",
                      retries=retries, base_wait=base_wait)
    return {k: (_resample_to_daily(v) if k in ("prices", "market_caps", "total_volumes") else v)
            for k, v in payload.items()}


def cache_key_for(coin: str | None, contract: str | None, platform: str) -> str:
    return coin if coin else f"contract_{platform}_{contract}"


def save_cache(cache_key: str, days: int, payload: dict) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / f"{cache_key}_{days}d.json"
    out_path.write_text(json.dumps(payload))
    return out_path


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--coin", help="CoinGecko coin id, e.g. solana, jupiter-exchange-solana")
    group.add_argument("--contract", help="Token contract/mint address (looked up on --platform)")
    ap.add_argument("--platform", default="solana", help="CoinGecko platform id for --contract lookups")
    ap.add_argument("--days", type=int, default=180, help="Number of days of history (max ~365 on free tier)")
    ap.add_argument("--vs", default="usd")
    args = ap.parse_args()

    cache_key = cache_key_for(args.coin, args.contract, args.platform)
    print(f"Fetching {args.days}d of {cache_key}/{args.vs} history from CoinGecko...")
    if args.contract:
        payload = fetch_market_chart_by_contract(args.contract, args.days, args.platform, args.vs)
    else:
        payload = fetch_market_chart(args.coin, args.days, args.vs)
    out_path = save_cache(cache_key, args.days, payload)
    n = len(payload.get("prices", []))
    print(f"Saved {n} price points to {out_path}")


if __name__ == "__main__":
    main()
