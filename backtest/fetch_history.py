#!/usr/bin/env python3
"""Fetch historical daily price/volume history and cache it locally as JSON,
for use by backtest/engine.py. Stdlib only, no API key required for either
source.

Two lookup modes:
  --pair <kraken-altname>   Kraken's own public OHLC endpoint, e.g. XBTUSD,
                             ETHUSD, SOLUSD -- the exchange-native source,
                             and the only way to get history for a pair with
                             no separate CoinGecko listing. Kraken's public
                             OHLC endpoint returns at most the most recent
                             720 daily candles (~2 years); --days above that
                             is silently capped.
  --coin <coingecko-id>     CoinGecko's market_chart endpoint, e.g. bitcoin,
                             ethereum -- useful for a longer history window
                             than Kraken's 720-candle cap allows, for majors
                             CoinGecko has deep history on.

Usage:
    python3 backtest/fetch_history.py --pair XBTUSD --days 180
    python3 backtest/fetch_history.py --coin bitcoin --days 365
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
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
KRAKEN_BASE = "https://api.kraken.com/0/public"
KRAKEN_MAX_DAILY_CANDLES = 720   # Kraken's public OHLC endpoint's own cap for interval=1440


def _resample_to_daily(series: list[list]) -> list[list]:
    """Collapses a [timestamp_ms, value] series down to one point per UTC
    calendar day (the last observation of that day = the day's close),
    regardless of the source granularity.

    Why this exists: CoinGecko's free market_chart endpoint auto-selects
    granularity by range -- hourly for any days<=90 request, daily only
    above 90. Every "daily-scale" indicator in this repo (SMA10/30, RSI14,
    the regime filter's N-day SMA, volatility_breakout's 20-bar lookback)
    assumes each series entry is one day. Left unresampled, a days<=90
    request silently turns a "30-day SMA" into a ~30-*hour* SMA -- wrong by
    roughly 24x, without erroring or looking obviously broken. Kraken's OHLC
    endpoint is requested at interval=1440 (already daily) so this is a
    no-op there in practice, but it's kept as the single resampling point
    both sources funnel through, so neither can silently regress this."""
    if not series:
        return series
    by_day: dict[str, list] = {}
    for point in series:
        day_key = datetime.fromtimestamp(point[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day_key] = point  # last observation of the day wins; dict preserves first-seen (chronological) key order
    return list(by_day.values())


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
    each a list of [unix_ms, value] pairs, looked up by CoinGecko coin id.
    Resampled to one point per UTC day regardless of what granularity the API
    returned -- see _resample_to_daily()."""
    payload = _fetch(f"{COINGECKO_BASE}/coins/{coin_id}/market_chart?vs_currency={vs_currency}&days={days}")
    return {k: (_resample_to_daily(v) if k in ("prices", "market_caps", "total_volumes") else v)
            for k, v in payload.items()}


def fetch_market_chart_by_pair(altname: str, days: int) -> dict:
    """Same payload shape as fetch_market_chart ({prices, total_volumes}, no
    market_caps -- Kraken's OHLC doesn't report market cap), sourced from
    Kraken's own public OHLC endpoint at daily (1440-minute) granularity.
    Capped at Kraken's own most-recent-720-candle limit for this interval."""
    days = min(days, KRAKEN_MAX_DAILY_CANDLES)
    payload = _fetch(f"{KRAKEN_BASE}/OHLC?pair={altname}&interval=1440")
    if payload.get("error"):
        raise RuntimeError(f"Kraken OHLC error for {altname}: {payload['error']}")
    result = payload.get("result") or {}
    candles = next((v for k, v in result.items() if k != "last"), None)
    if not candles:
        raise RuntimeError(f"Kraken OHLC returned no candles for {altname}")
    candles = candles[-days:]
    prices = [[int(c[0]) * 1000, float(c[4])] for c in candles]              # [ms, close]
    total_volumes = [[int(c[0]) * 1000, float(c[6]) * float(c[4])] for c in candles]  # [ms, base_volume * close] as a rough USD-notional proxy
    return {
        "prices": _resample_to_daily(prices),
        "total_volumes": _resample_to_daily(total_volumes),
    }


def cache_key_for(coin: str | None, pair: str | None) -> str:
    return coin if coin else f"kraken_{pair}"


def save_cache(cache_key: str, days: int, payload: dict) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / f"{cache_key}_{days}d.json"
    out_path.write_text(json.dumps(payload))
    return out_path


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--pair", help="Kraken pair altname, e.g. XBTUSD, ETHUSD, SOLUSD")
    group.add_argument("--coin", help="CoinGecko coin id, e.g. bitcoin, ethereum")
    ap.add_argument("--days", type=int, default=180, help="Number of days of history (Kraken --pair capped at 720; CoinGecko --coin max ~365 on free tier)")
    args = ap.parse_args()

    cache_key = cache_key_for(args.coin, args.pair)
    print(f"Fetching {args.days}d of {cache_key} history from {'Kraken' if args.pair else 'CoinGecko'}...")
    if args.pair:
        payload = fetch_market_chart_by_pair(args.pair, args.days)
    else:
        payload = fetch_market_chart(args.coin, args.days)
    out_path = save_cache(cache_key, args.days, payload)
    n = len(payload.get("prices", []))
    print(f"Saved {n} price points to {out_path}")


if __name__ == "__main__":
    main()
