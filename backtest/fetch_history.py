#!/usr/bin/env python3
"""Fetch historical daily price history and cache it locally as JSON, for
use by backtest/engine.py.

Two sources:
  --kraken-pair <pair>     e.g. XBTUSD, ETHUSD, SOLUSD -- the primary source
                            now that this project trades Kraken. No API key
                            (Kraken's OHLC endpoint is public), and Kraken's
                            candles are already daily at interval=1440, so no
                            resampling is needed (contrast --coin below).
  --coin <coingecko-id>     e.g. bitcoin, ethereum -- kept only for the
                            market-cap-based tier lookup in
                            research/discover_candidates.py's
                            fetch_market_caps_usd(), which needs CoinGecko
                            since Kraken has no market-cap field at all.
                            (The old --contract-by-Solana-mint mode is gone
                            with the Solana pipeline it existed for.)

Usage:
    python3 backtest/fetch_history.py --kraken-pair XBTUSD --days 180
    python3 backtest/fetch_history.py --coin bitcoin --days 180
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kraken import client as kc  # noqa: E402

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


def fetch_ohlc_kraken(pair: str, days: int, interval_minutes: int = kc.OHLC_DAILY_INTERVAL_MINUTES,
                       retries: int = 4, backoff: float = 10.0) -> dict:
    """Returns the same {"prices": [[ts_ms, close], ...]} shape
    fetch_market_chart returns, sourced from Kraken's own public OHLC
    endpoint (kraken/client.py's ohlc()) instead of CoinGecko. Already
    at-the-requested-granularity candles -- no resampling needed, unlike
    the CoinGecko path. This is the primary price-history source for
    backtesting/paper-trading now that this project trades Kraken pairs
    directly.

    interval_minutes defaults to daily (unchanged behavior for every
    existing caller) -- pass e.g. 60/15/5 for day-trading-timeframe
    strategies. See kraken.client.ohlc()'s docstring for the real ceiling
    on how much history a short interval can actually return (Kraken's
    OHLC endpoint caps at ~720 bars regardless of interval).

    retries/backoff default to kraken.client.ohlc()'s own patient default
    (kept in sync by hand, same convention as that function's docstring) --
    a caller must explicitly override to get paper_trading's tight-cron-loop
    impatience, matching get_price_history_closes()'s call. Code review
    (2026-08-26) found this wrapper previously shadowed kc.ohlc()'s default
    with its own separate, more impatient one (3 retries/2.0s vs. 4/10.0s),
    so fixing only kc.ohlc()'s default without also fixing this one would
    NOT have actually changed backtest_all.py's behavior at all."""
    return {"prices": kc.ohlc(pair, days, interval_minutes=interval_minutes, retries=retries, backoff=backoff)}


def cache_key_for_kraken(pair: str, interval_minutes: int = kc.OHLC_DAILY_INTERVAL_MINUTES) -> str:
    """Daily (the original, still-default granularity) keeps the original
    unsuffixed cache key -- every existing cache file and every test/doc
    reference to `kraken_<PAIR>` stays valid. Any other interval gets its
    own suffixed key (e.g. `kraken_XBTUSD_60m`) so different timeframes of
    the same pair never collide in backtest/cache/."""
    if interval_minutes == kc.OHLC_DAILY_INTERVAL_MINUTES:
        return f"kraken_{pair}"
    return f"kraken_{pair}_{interval_minutes}m"


def save_cache(cache_key: str, days: int, payload: dict) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / f"{cache_key}_{days}d.json"
    out_path.write_text(json.dumps(payload))
    return out_path


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--kraken-pair", help="Kraken pair, e.g. XBTUSD, ETHUSD, SOLUSD (primary source)")
    group.add_argument("--coin", help="CoinGecko coin id, e.g. bitcoin, ethereum -- for mcap lookups only")
    ap.add_argument("--days", type=int, default=180, help="Number of days of history")
    ap.add_argument("--interval-minutes", type=int, default=kc.OHLC_DAILY_INTERVAL_MINUTES,
                     choices=sorted(kc.OHLC_VALID_INTERVALS_MINUTES),
                     help="Only used with --kraken-pair. Default 1440 (daily). A short interval has a real ceiling "
                          "on obtainable history regardless of --days -- see kraken.client.ohlc()'s docstring "
                          "(e.g. 5-minute bars: ~2.5 days max, 15-minute: ~7.5 days, hourly: ~30 days).")
    ap.add_argument("--vs", default="usd", help="Only used with --coin -- Kraken pairs are already USD-quoted")
    args = ap.parse_args()

    if args.kraken_pair:
        cache_key = cache_key_for_kraken(args.kraken_pair, args.interval_minutes)
        print(f"Fetching {args.days}d of {args.kraken_pair} history from Kraken ({args.interval_minutes}min bars)...")
        payload = fetch_ohlc_kraken(args.kraken_pair, args.days, interval_minutes=args.interval_minutes)
    else:
        cache_key = args.coin
        print(f"Fetching {args.days}d of {cache_key}/{args.vs} history from CoinGecko...")
        payload = fetch_market_chart(args.coin, args.days, args.vs)
    out_path = save_cache(cache_key, args.days, payload)
    n = len(payload.get("prices", []))
    print(f"Saved {n} price points to {out_path}")


if __name__ == "__main__":
    main()
