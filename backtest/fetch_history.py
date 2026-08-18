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
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"
BASE_URL = "https://api.coingecko.com/api/v3"


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "claude-agent-trader/1.0"})
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = 10 * (attempt + 1)
                print(f"Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            raise
    raise last_err


def fetch_market_chart(coin_id: str, days: int, vs_currency: str = "usd") -> dict:
    """Returns CoinGecko's market_chart payload: {prices, market_caps, total_volumes},
    each a list of [unix_ms, value] pairs, looked up by CoinGecko coin id."""
    return _fetch(f"{BASE_URL}/coins/{coin_id}/market_chart?vs_currency={vs_currency}&days={days}")


def fetch_market_chart_by_contract(contract: str, days: int, platform: str = "solana",
                                    vs_currency: str = "usd") -> dict:
    """Same payload shape as fetch_market_chart, looked up by token contract/mint
    address instead of a CoinGecko coin id. Works for tokens that were never
    given a curated CoinGecko listing (confirmed live against a same-day
    pump.fun launch) -- this is what makes freshly-discovered tokens
    backtestable without waiting for CoinGecko to index them by name."""
    return _fetch(f"{BASE_URL}/coins/{platform}/contract/{contract}/market_chart?vs_currency={vs_currency}&days={days}")


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
