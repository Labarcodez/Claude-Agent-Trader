#!/usr/bin/env python3
"""Fetch historical daily price/volume history from CoinGecko's free public API
and cache it locally as JSON, for use by backtest/engine.py.

No API key required (uses the public api.coingecko.com endpoints, which are
rate-limited but sufficient for periodic backtesting). Stdlib only.

Usage:
    python3 backtest/fetch_history.py --coin solana --days 180
    python3 backtest/fetch_history.py --coin jupiter-exchange-solana --days 90
"""
import argparse
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"
BASE_URL = "https://api.coingecko.com/api/v3"


def fetch_market_chart(coin_id: str, days: int, vs_currency: str = "usd") -> dict:
    """Returns CoinGecko's market_chart payload: {prices, market_caps, total_volumes},
    each a list of [unix_ms, value] pairs."""
    url = f"{BASE_URL}/coins/{coin_id}/market_chart?vs_currency={vs_currency}&days={days}"
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


def save_cache(coin_id: str, days: int, payload: dict) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / f"{coin_id}_{days}d.json"
    out_path.write_text(json.dumps(payload))
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True, help="CoinGecko coin id, e.g. solana, jupiter-exchange-solana")
    ap.add_argument("--days", type=int, default=180, help="Number of days of history (max ~365 on free tier)")
    ap.add_argument("--vs", default="usd")
    args = ap.parse_args()

    print(f"Fetching {args.days}d of {args.coin}/{args.vs} history from CoinGecko...")
    payload = fetch_market_chart(args.coin, args.days, args.vs)
    out_path = save_cache(args.coin, args.days, payload)
    n = len(payload.get("prices", []))
    print(f"Saved {n} price points to {out_path}")


if __name__ == "__main__":
    main()
