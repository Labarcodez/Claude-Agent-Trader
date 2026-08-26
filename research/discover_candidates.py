#!/usr/bin/env python3
"""Dynamic Kraken pair discovery + automated liquidity/spread scoring,
stdlib only.

Replaces a hand-maintained watchlist: pulls every tradable pair from
Kraken's public AssetPairs endpoint, cross-references live 24h volume/spread
from the Ticker endpoint, and evaluates each one against liquidity and
execution-quality thresholds -- no human has to add a pair by name first.

Unlike the project's original Solana/Jupiter version, there's no on-chain
rug-pull surface to check: every Kraken-listed pair has already gone
through Kraken's own listing/compliance review, which is the trust anchor
here. What still needs live, per-cycle checking is liquidity (24h quote
volume) and execution quality (bid/ask spread), because both move fast and
a pair that was liquid last week can thin out well before Kraken ever
delists it.

Both endpoints used here are public, free, and require no API key:
    https://api.kraken.com/0/public/AssetPairs
    https://api.kraken.com/0/public/Ticker

See config/discovery.yaml for the human-readable version of these
thresholds (keep the two in sync by hand -- this script does not parse that
file, to stay dependency-free) and docs/STRATEGY.md "Autonomous discovery"
for the reasoning.

Usage:
    python3 research/discover_candidates.py
    python3 research/discover_candidates.py --max-candidates 30 --min-24h-quote-volume-usd 1000000
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from account.kraken_common import LEGACY_ASSET_PREFIX  # noqa: E402 -- single source of truth, shared with account/portfolio.py

KRAKEN_BASE = "https://api.kraken.com/0/public"
USER_AGENT = "claude-agent-trader-discovery/1.0"

RESULTS_DIR = Path(__file__).parent / "results"

ALLOWED_QUOTE_CURRENCIES = {"USD", "USDT", "USDC"}
REQUIRED_PAIR_STATUS = "online"
LEVERAGED_TOKEN_PATTERN = re.compile(r"(2L|2S|3L|3S|4L|4S|5L|5S)$")

# Batched Ticker calls -- keeps individual request URLs small and is a good
# citizen to a shared public endpoint, same spirit as the old Jupiter/
# RugCheck request pacing even though Kraken's published rate limits for
# public market-data calls are generous.
TICKER_BATCH_SIZE = 40


def _get_json(url: str, retries: int = 3, backoff: float = 2.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("error"):
                # Kraken's REST API returns HTTP 200 with an "error" array on
                # invalid requests -- treat a non-empty one as a failure.
                print(f"  ! Kraken API error: {payload['error']} ({url})", file=sys.stderr)
                return None
            return payload.get("result")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(backoff * (attempt + 1))
                last_err = e
                continue
            if e.code == 404:
                return None
            last_err = e
            break
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(backoff)
    print(f"  ! request failed: {url} ({last_err})", file=sys.stderr)
    return None


def fetch_asset_pairs() -> dict[str, dict]:
    """Returns {altname: pair_info} for every pair Kraken's AssetPairs
    endpoint reports, keyed by the human-readable altname (e.g. "XBTUSD")
    rather than the raw Kraken pair key (e.g. "XXBTZUSD"), since altname is
    what's unambiguous across Kraken's legacy/new asset-code quirks."""
    result = _get_json(f"{KRAKEN_BASE}/AssetPairs")
    if not result:
        return {}
    by_altname: dict[str, dict] = {}
    for _pair_key, info in result.items():
        altname = info.get("altname")
        if altname:
            by_altname[altname] = info
    return by_altname


def fetch_ticker_batch(altnames: list[str]) -> dict[str, dict]:
    """Ticker's `pair` query param accepts Kraken pair keys OR altnames
    interchangeably; batched to keep each request modest in size."""
    out: dict[str, dict] = {}
    for i in range(0, len(altnames), TICKER_BATCH_SIZE):
        batch = altnames[i:i + TICKER_BATCH_SIZE]
        result = _get_json(f"{KRAKEN_BASE}/Ticker?pair={','.join(batch)}")
        if result:
            out.update(result)
    return out


def gather_candidates(args) -> dict[str, dict]:
    """Returns {altname: {"pair": pair_info, "ticker": ticker_info}} for
    every online, allowed-quote-currency, non-leveraged-token pair, ranked
    by 24h notional volume (last trade price * 24h base volume) so the
    max-candidates cap keeps the most liquid pairs first."""
    pairs = fetch_asset_pairs()
    if not pairs:
        return {}

    prefiltered: dict[str, dict] = {}
    for altname, info in pairs.items():
        if info.get("status") != REQUIRED_PAIR_STATUS:
            continue
        quote = info.get("quote", "")
        quote_norm = LEGACY_ASSET_PREFIX.sub("", quote)
        if quote_norm not in ALLOWED_QUOTE_CURRENCIES:
            continue
        base_altname = (info.get("base") or "")
        base_norm = LEGACY_ASSET_PREFIX.sub("", base_altname)
        if LEVERAGED_TOKEN_PATTERN.search(altname) or LEVERAGED_TOKEN_PATTERN.search(base_norm):
            continue
        prefiltered[altname] = info

    tickers = fetch_ticker_batch(list(prefiltered.keys()))

    def rough_notional(altname: str) -> float:
        t = tickers.get(altname)
        if not t:
            return 0.0
        try:
            last_price = float(t["c"][0])
            vol_24h = float(t["v"][1])
            return last_price * vol_24h
        except (KeyError, IndexError, ValueError, TypeError):
            return 0.0

    ranked = sorted(prefiltered.keys(), key=rough_notional, reverse=True)
    candidates = {}
    for altname in ranked[: args.max_candidates]:
        candidates[altname] = {"pair": prefiltered[altname], "ticker": tickers.get(altname)}
    return candidates


def classify_tier(quote_volume_24h_usd: float, spread_bps: float, args) -> str:
    if quote_volume_24h_usd >= args.blue_chip_min_volume_usd and spread_bps <= args.blue_chip_max_spread_bps:
        return "blue_chip"
    if quote_volume_24h_usd >= args.established_min_volume_usd and spread_bps <= args.established_max_spread_bps:
        return "established"
    return "emerging"   # includes small/mid-cap alts and anything else that still clears every safety check


def evaluate_candidate(altname: str, candidate: dict, args) -> dict:
    """Two data sources, both already fetched for free: Kraken's own
    AssetPairs (status, quote currency, order-size minimums, margin
    availability) and Ticker (last price, 24h volume/vwap, top-of-book
    bid/ask for spread). No paid/rate-limited third-party lookups needed --
    Kraken's own market data is the whole picture for a CEX pair, unlike the
    on-chain audit trail a DEX-listed token needs."""
    reasons_fail: list[str] = []
    pair_info = candidate.get("pair") or {}
    ticker = candidate.get("ticker")

    base = LEGACY_ASSET_PREFIX.sub("", pair_info.get("base") or "")
    quote = LEGACY_ASSET_PREFIX.sub("", pair_info.get("quote") or "")
    wsname = pair_info.get("wsname")
    status = pair_info.get("status")
    ordermin = pair_info.get("ordermin")
    costmin = pair_info.get("costmin")
    # Order-construction precision -- see account/precision.py, which rounds
    # a proposed order's price/volume to exactly these before it's ever sent
    # to Kraken, and clamp_to_pair_minimums(), which uses ordermin/costmin
    # above. Surfaced here so trade-cycle never has to re-fetch AssetPairs
    # itself just to place an order for something this cycle already found.
    pair_decimals = pair_info.get("pair_decimals")
    lot_decimals = pair_info.get("lot_decimals")
    margin_allowed = bool(pair_info.get("leverage_buy")) or bool(pair_info.get("leverage_sell"))

    precision_data = {
        "ordermin": ordermin, "costmin": costmin,
        "pair_decimals": pair_decimals, "lot_decimals": lot_decimals,
        "margin_allowed": margin_allowed,
    }

    if status != REQUIRED_PAIR_STATUS:
        reasons_fail.append(f"pair status '{status}' != '{REQUIRED_PAIR_STATUS}'")

    if ticker is None:
        reasons_fail.append("no ticker data returned -- can't confirm live price/volume/spread")
        return {
            "altname": altname, "base": base, "quote": quote, "wsname": wsname,
            "eligible": False, "tier": None, "reasons_fail": reasons_fail,
            "data": {"status": status, **precision_data},
        }

    try:
        last_price = float(ticker["c"][0])
        bid = float(ticker["b"][0])
        ask = float(ticker["a"][0])
        vol_24h_base = float(ticker["v"][1])
        vwap_24h = float(ticker["p"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        reasons_fail.append("malformed ticker payload -- can't parse price/volume fields")
        return {
            "altname": altname, "base": base, "quote": quote, "wsname": wsname,
            "eligible": False, "tier": None, "reasons_fail": reasons_fail,
            "data": {"status": status, **precision_data},
        }

    quote_volume_24h_usd = vol_24h_base * vwap_24h   # vwap_24h is already in quote-currency terms; USD/USDT/USDC all treated ~1:1 with USD for sizing purposes
    mid = (bid + ask) / 2 if (bid and ask) else None
    spread_bps = ((ask - bid) / mid * 10_000) if (mid and mid > 0) else None

    if last_price < args.min_price_usd:
        reasons_fail.append(f"last price {last_price} < min ${args.min_price_usd} -- likely bad/stale data")
    if quote_volume_24h_usd < args.min_24h_quote_volume_usd:
        reasons_fail.append(f"24h quote volume ${quote_volume_24h_usd:,.0f} < min ${args.min_24h_quote_volume_usd:,.0f}")
    if spread_bps is None:
        reasons_fail.append("no valid bid/ask -- can't confirm spread")
    elif spread_bps > args.max_spread_bps:
        reasons_fail.append(f"spread {spread_bps:.1f}bps > max {args.max_spread_bps}bps")

    tier = classify_tier(quote_volume_24h_usd, spread_bps, args) if (not reasons_fail and spread_bps is not None) else None

    return {
        "altname": altname,
        "base": base,
        "quote": quote,
        "wsname": wsname,
        "eligible": len(reasons_fail) == 0,
        "tier": tier,
        "reasons_fail": reasons_fail,
        "data": {
            "status": status,
            "last_price_usd": last_price,
            "bid": bid,
            "ask": ask,
            "spread_bps": spread_bps,
            "volume_24h_base": vol_24h_base,
            "vwap_24h": vwap_24h,
            "quote_volume_24h_usd": quote_volume_24h_usd,
            **precision_data,
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-candidates", type=int, default=60,
                     help="Cap on unique pairs evaluated per run (most-liquid-first) -- keep in sync with config/discovery.yaml's max_candidates_per_cycle")
    # Safety thresholds -- keep in sync with config/discovery.yaml's `safety` block
    ap.add_argument("--min-24h-quote-volume-usd", dest="min_24h_quote_volume_usd", type=float, default=500_000)
    ap.add_argument("--max-spread-bps", type=float, default=50)
    ap.add_argument("--min-price-usd", type=float, default=0.000001)
    # Tier thresholds -- keep in sync with config/discovery.yaml's `tiers` block
    ap.add_argument("--blue-chip-min-volume-usd", dest="blue_chip_min_volume_usd", type=float, default=100_000_000)
    ap.add_argument("--blue-chip-max-spread-bps", dest="blue_chip_max_spread_bps", type=float, default=10)
    ap.add_argument("--established-min-volume-usd", dest="established_min_volume_usd", type=float, default=10_000_000)
    ap.add_argument("--established-max-spread-bps", dest="established_max_spread_bps", type=float, default=25)
    args = ap.parse_args()

    print("Fetching tradable pairs from Kraken AssetPairs + Ticker...")
    candidates = gather_candidates(args)
    print(f"{len(candidates)} candidates evaluated (most-liquid-first, capped at --max-candidates={args.max_candidates}).\n")

    eligible, rejected = [], []
    for i, altname in enumerate(candidates, 1):
        print(f"  [{i}/{len(candidates)}] {altname}")
        result = evaluate_candidate(altname, candidates[altname], args)
        (eligible if result["eligible"] else rejected).append(result)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"discovery_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps({"eligible": eligible, "rejected": rejected}, indent=2))

    print(f"\n{'=' * 70}\nELIGIBLE ({len(eligible)}):")
    for r in eligible:
        d = r["data"]
        print(f"  {r['altname']:>12}  tier={r['tier']:<10} 24hVol=${d['quote_volume_24h_usd']:>14,.0f}  "
              f"spread={d['spread_bps']:>6.1f}bps  price=${d['last_price_usd']:g}")

    print(f"\nREJECTED ({len(rejected)}):")
    for r in rejected:
        print(f"  {r['altname']:>12}  {'; '.join(r['reasons_fail'][:2])}")

    print(f"\nFull report (all fields, all reasons) saved to {out_path}")
    print("\nEligible pairs are candidates for the trade-cycle skill's signal step, not")
    print("automatic buys -- position sizing/risk checks in config/risk.yaml still apply,")
    print("and the exact tradable symbol Kraken CLI/MCP expects should be re-verified at")
    print("execution time (see .claude/skills/trade-cycle/SKILL.md) rather than assumed")
    print("identical to this script's altname.")


if __name__ == "__main__":
    main()
