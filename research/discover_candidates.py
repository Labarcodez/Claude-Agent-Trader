#!/usr/bin/env python3
"""Kraken spot-pair discovery + safety scoring, stdlib only (+ kraken/client.py).

Replaces the earlier Solana/Jupiter/RugCheck pipeline (see git history) now
that this project trades Kraken instead -- see docs/KRAKEN_SETUP.md and the
plan recorded 2026-08-25 for why. The safety model shifts accordingly:
Kraken already reviews and vets every asset before listing it (delisting
risk, not rug-pull risk), so there is no on-chain audit data, no
mint/freeze-authority check, and no RugCheck-style "is this a confirmed
scam" cross-check to run here -- those questions don't apply to an
already-listed, centralized-exchange pair the way they did to an arbitrary
Solana contract address. What DOES still need checking on Kraken is
tradability and exit liquidity: is the pair actually online, is there
enough 24h volume to get back out of a position without moving the price
yourself, and is the bid/ask spread tight enough that entering and exiting
doesn't hand away the edge to the spread alone.

Kraken's AssetPairs + Ticker endpoints are both public (no API key), free,
and return every field this script needs in two bulk calls -- no
per-candidate secondary lookups (contrast the old pipeline's per-candidate
RugCheck call), so there's no rate-limit-driven need to sample a subset of
the universe or rotate through it across cycles: every USD-quoted pair
Kraken lists (a few hundred, not a few thousand) is evaluated every run.

The one thing Kraken doesn't expose at all is market cap (used for
blue_chip/established tier classification) -- that still comes from
CoinGecko's public markets endpoint, looked up in bulk for a small curated
list of Kraken-listed majors (KRAKEN_TO_COINGECKO_ID below), not per
candidate.

Usage:
    python3 research/discover_candidates.py
    python3 research/discover_candidates.py --min-24h-volume-usd 500000 --max-spread-bps 50
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kraken import client as kc  # noqa: E402

# Discovered pair/asset names are always plain ASCII on Kraken (unlike the old
# Solana pipeline's arbitrary on-chain symbols), but keep this guard anyway --
# cheap insurance against the exact class of Windows-console crash documented
# in git history, and harmless if it never triggers here.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
USER_AGENT = "claude-agent-trader-discovery/1.0"

RESULTS_DIR = Path(__file__).parent / "results"
DEFAULT_DISCOVERY_HISTORY_PATH = Path(__file__).parent.parent / "state" / "discovery_history.json"
MAX_HISTORY_SNAPSHOTS_PER_MINT = 20  # kept as a rolling trend window, same reasoning as before

# Kraken base-asset altname -> CoinGecko coin id, for the market-cap lookup
# classify_tier() needs (Kraken's own API has no market-cap field at all).
# Deliberately a curated, hand-checked list rather than an auto-derived one --
# Kraken's asset codes (e.g. "XBT" for Bitcoin, "XETH" for Ethereum) don't
# follow one consistent convention, and a wrong auto-mapping would silently
# tier-misclassify a real position. Anything not listed here falls back to
# "emerging" in classify_tier() -- the same conservative-default posture the
# old pipeline used for a token with no reported mcap.
KRAKEN_TO_COINGECKO_ID: dict[str, str] = {
    "XBT": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "ADA": "cardano", "DOGE": "dogecoin", "XDG": "dogecoin",  # Kraken's own altname for Dogecoin is XDG, not DOGE
    "LTC": "litecoin", "DOT": "polkadot",
    "LINK": "chainlink", "MATIC": "matic-network", "POL": "matic-network",
    "AVAX": "avalanche-2", "ATOM": "cosmos", "UNI": "uniswap", "AAVE": "aave",
    "BCH": "bitcoin-cash", "ETC": "ethereum-classic", "XLM": "stellar",
    "ALGO": "algorand", "FIL": "filecoin", "SHIB": "shiba-inu", "NEAR": "near",
    "APT": "aptos", "ARB": "arbitrum", "OP": "optimism", "SUI": "sui",
    "INJ": "injective-protocol", "RENDER": "render-token", "TIA": "celestia",
    "PEPE": "pepe", "TRX": "tron", "XMR": "monero", "XTZ": "tezos",
    "EOS": "eos", "MANA": "decentraland", "SAND": "the-sandbox", "GRT": "the-graph",
    "CRV": "curve-dao-token", "MKR": "maker", "COMP": "compound-governance-token",
    "SNX": "havven", "LDO": "lido-dao", "FET": "fetch-ai", "WIF": "dogwifcoin",
    "BONK": "bonk", "JUP": "jupiter-exchange-solana", "PYTH": "pyth-network",
}

# Kraken keeps dark-pool pairs (hidden order book, invite-only liquidity) at
# the same asset pair with a ".d" suffix (e.g. "XBTUSD.d") -- these aren't
# accessible the way a normal listed pair is, and must never be treated as
# an ordinary discovery candidate.
DARK_POOL_SUFFIX = ".d"

# Fiat currencies (Kraken prefixes every fiat asset code with "Z" -- ZUSD,
# ZEUR, ZGBP, ...) and USD-pegged stablecoins quoted against USD aren't
# meaningful trading candidates -- "buying" USDTUSD is just holding a dollar
# under a different name, the same reason the old Solana pipeline excluded
# SOL/USDC as settlement assets rather than candidates (config/core_assets.yaml).
# Excluded by base asset code, checked before the KRAKEN_TO_COINGECKO_ID
# lookup/stripping logic below.
EXCLUDED_BASE_ASSETS = {
    "ZEUR", "ZGBP", "ZJPY", "ZCAD", "ZAUD", "ZCHF", "ZUSD",
    "USDT", "USDC", "DAI", "PYUSD", "USDG", "TUSD", "USDS", "RLUSD", "GUSD", "EURT",
}

# 429 (rate limited) and 502/503/504 are transient and worth retrying; a
# definitive client error (400/401/403/404) isn't. Mirrors
# kraken/client.py's RETRYABLE_HTTP_CODES for this file's one remaining
# non-Kraken upstream (CoinGecko, for market caps only).
RETRYABLE_HTTP_CODES = {429, 502, 503, 504}


def _get_json(url: str, retries: int = 3, backoff: float = 2.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_CODES:
                time.sleep(backoff * (attempt + 1))
                last_err = e
                continue
            if e.code == 404:
                return None
            last_err = e
            break
        except OSError as e:
            # Broad on purpose -- see kraken/client.py's _request() for why
            # (confirmed live elsewhere in this project: a bare
            # http.client.RemoteDisconnected instead of a wrapped URLError).
            last_err = e
            time.sleep(backoff)
    print(f"  ! request failed: {url} ({last_err})", file=sys.stderr)
    return None


def fetch_market_caps_usd(coingecko_ids: list[str]) -> dict[str, float]:
    """{coingecko_id: market_cap_usd} in as few CoinGecko calls as possible --
    the /coins/markets endpoint accepts a comma-separated ids list, so this
    is one call for the whole KRAKEN_TO_COINGECKO_ID table regardless of how
    many candidates this cycle actually needs tiered."""
    if not coingecko_ids:
        return {}
    ids_param = ",".join(dict.fromkeys(coingecko_ids))
    data = _get_json(f"{COINGECKO_BASE}/coins/markets?vs_currency=usd&ids={ids_param}") or []
    return {row["id"]: row.get("market_cap") or 0 for row in data if isinstance(row, dict) and row.get("id")}


def fetch_kraken_asset_altnames() -> dict[str, str]:
    """{raw_asset_code: altname} for every asset Kraken lists (e.g.
    "XXBT" -> "XBT", "ZUSD" -> "USD") -- the authoritative source for the
    same normalization _kraken_base_altname()'s heuristic approximates.
    Fetched once per gather_candidates() call (a single bulk call, ~836
    assets as of writing) rather than per-pair. Fail-safe: an empty dict on
    any error, which makes every _kraken_base_altname() call fall through
    to its heuristic -- never the reason discovery crashes."""
    try:
        return {code: info.get("altname") for code, info in kc.assets().items() if info.get("altname")}
    except (kc.KrakenAPIError, OSError):
        return {}


def gather_candidates(args) -> dict[str, dict]:
    """Returns {pair_name: merged_asset_pair_and_ticker_data}. Every
    online, USD-quoted, non-dark-pool pair Kraken lists -- no sampling, no
    per-source dedup needed (AssetPairs is already the deduped universe)."""
    pairs_info = kc.asset_pairs()
    usd_pair_names = [
        name for name, info in pairs_info.items()
        if info.get("status") == "online"
        and info.get("quote") in ("ZUSD", "USD")
        and not name.endswith(DARK_POOL_SUFFIX)
        and info.get("base") not in EXCLUDED_BASE_ASSETS
    ]
    tickers = kc.ticker(usd_pair_names)
    asset_altnames = fetch_kraken_asset_altnames()

    candidates: dict[str, dict] = {}
    for name in usd_pair_names:
        t = tickers.get(name)
        if not t:
            continue  # Ticker didn't return this pair (rare transient gap) -- skip rather than evaluate on partial data
        info = pairs_info[name]
        ask = float(t["a"][0])
        bid = float(t["b"][0])
        last = float(t["c"][0])
        vol_24h_base = float(t["v"][1])
        vwap_24h = float(t["p"][1])
        mid = (ask + bid) / 2 if (ask and bid) else last
        base = info.get("base")
        candidates[name] = {
            "pair": name,
            "base": base,
            # The authoritative Kraken-code -> altname mapping (e.g. "XXBT"
            # -> "XBT"), computed once here rather than by every caller
            # re-deriving it from a heuristic -- see _kraken_base_altname()
            # for why a per-caller heuristic was a real, code-review-found
            # risk (a wrong tier classification with no error raised).
            "base_altname": _kraken_base_altname(base, asset_altnames),
            "altname": info.get("altname"),
            "wsname": info.get("wsname"),
            "status": info.get("status"),
            "price_usd": mid,
            "last_trade_price_usd": last,
            "ask_usd": ask,
            "bid_usd": bid,
            "volume_24h_base": vol_24h_base,
            "volume_24h_usd": vol_24h_base * vwap_24h if vwap_24h else vol_24h_base * mid,
            # Requires a REAL ask AND bid, not just a nonzero mid -- mid
            # falls back to last-trade price when the book is empty
            # (ask==bid==0), and (0-0)/last computes a false "perfectly
            # tight" 0.0bps spread for a pair with no live quote at all,
            # instead of the missing-data signal evaluate_candidate()'s
            # safety gate is supposed to catch. Real bug, found by code
            # review: this let a stale/one-sided book sail through the
            # spread check as if it were the tightest market on Kraken.
            "spread_bps": ((ask - bid) / mid) * 10_000 if (ask and bid and mid) else None,
        }
    return candidates


def market_caps_for_pairs(candidates: dict[str, dict], pairs: list[str]) -> dict[str, float]:
    """Bulk market-cap lookup for `pairs`, keyed by PAIR name (not
    coingecko id) for direct use by callers. Consolidates what used to be
    three near-identical hand-written copies of this block (this module's
    own main(), backtest/backtest_all.py, paper_trading/run_paper_cycle.py)
    -- code review (2026-08-26) found they'd already started diverging
    (one included held positions outside the volume-rank cap, the others
    didn't), a real drift risk for logic that determines position sizing
    via classify_tier(). Returns 0 for any pair whose base asset isn't in
    KRAKEN_TO_COINGECKO_ID, matching classify_tier()'s existing "emerging"
    fallback for an unknown/zero mcap."""
    coingecko_ids = [
        KRAKEN_TO_COINGECKO_ID[candidates[p]["base_altname"]]
        for p in pairs if candidates[p].get("base_altname") in KRAKEN_TO_COINGECKO_ID
    ]
    market_caps_by_id = fetch_market_caps_usd(coingecko_ids)
    result = {}
    for p in pairs:
        cg_id = KRAKEN_TO_COINGECKO_ID.get(candidates[p].get("base_altname"))
        result[p] = market_caps_by_id.get(cg_id, 0) if cg_id else 0
    return result


def _kraken_base_altname(base_code: str | None, asset_altnames: dict[str, str] | None = None) -> str:
    """Normalizes a raw Kraken asset code (e.g. "XXBT") to its altname (e.g.
    "XBT") -- KRAKEN_TO_COINGECKO_ID is keyed by altname. Prefers Kraken's
    own authoritative Assets-endpoint mapping (`asset_altnames`, from
    fetch_kraken_asset_altnames() -- gather_candidates() fetches this once
    and passes it through) when available.

    Falls back to a length/prefix heuristic ("strip a leading X or Z if the
    remaining string is a recognizable code") only when no authoritative
    map is available (e.g. a direct/test call, or the Assets call itself
    failed) -- kept for exactly that fallback case, not as the primary
    path anymore. Code review (2026-08-26) found the heuristic alone,
    reimplemented independently by 3+ call sites, could silently
    mis-normalize a code that doesn't fit its assumed shape, mis-tiering a
    real position with no error raised -- the authoritative map doesn't
    have that failure mode since it's Kraken's own data, not a guess.

    None-safe (a pair with a missing `base` field degrades to "emerging"
    tier via a lookup miss, not an uncaught TypeError)."""
    if base_code is None:
        return ""
    if asset_altnames and base_code in asset_altnames:
        return asset_altnames[base_code]
    if base_code in KRAKEN_TO_COINGECKO_ID:
        return base_code
    if len(base_code) > 3 and base_code[0] in ("X", "Z"):
        stripped = base_code[1:]
        if stripped in KRAKEN_TO_COINGECKO_ID:
            return stripped
    return base_code


def classify_tier(mcap_usd: float, volume_24h_usd: float, args) -> str:
    """Kraken has no holder-count or on-chain-verification signal, so tiering
    here rests on two things a centralized exchange DOES report cleanly:
    market cap (via CoinGecko, see fetch_market_caps_usd) and this pair's
    own 24h USD volume (via Kraken's Ticker, already in `data` before this
    is called) -- both must clear their tier's bar, mirroring the old
    pipeline's "mcap AND holder_count" two-factor requirement."""
    if mcap_usd >= args.blue_chip_mcap_usd and volume_24h_usd >= args.blue_chip_volume_usd:
        return "blue_chip"
    if mcap_usd >= args.established_mcap_usd and volume_24h_usd >= args.established_volume_usd:
        return "established"
    return "emerging"  # everything that passes safety but isn't large/liquid enough yet


def load_discovery_history(path: Path = DEFAULT_DISCOVERY_HISTORY_PATH) -> dict:
    """Fail-safe, not fail-crash -- see the original pipeline's identical
    reasoning: a missing/corrupted cache just means trend-tracking restarts
    this cycle, never a reason to crash a discovery run."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}


def save_discovery_history(history: dict, path: Path = DEFAULT_DISCOVERY_HISTORY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    tmp_path.replace(path)  # atomic swap


def record_and_compute_trend(pair: str, symbol: str, data: dict, history: dict, now: datetime | None = None) -> dict:
    """Same shape/purpose as the original pipeline's version, with
    holder_count/organic_score/liquidity_usd replaced by this venue's own
    signals: 24h USD volume (growth = more real trading interest) and
    spread (a narrowing spread over time means the pair is getting easier,
    not harder, to trade in and out of)."""
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()
    entry = history.setdefault(pair, {"symbol": symbol, "snapshots": []})
    entry["symbol"] = symbol
    snapshots = entry["snapshots"]

    trend = {
        "cycles_seen": len(snapshots) + 1,
        "first_seen_at": snapshots[0]["ts"] if snapshots else now_iso,
        "volume_growth_usd_per_hour": None,
        "spread_bps_delta": None,
    }
    if snapshots:
        baseline = snapshots[0]
        hours_elapsed = None
        try:
            baseline_ts = datetime.fromisoformat(str(baseline.get("ts")))
            hours_elapsed = (now - baseline_ts).total_seconds() / 3600
        except (ValueError, TypeError):
            pass
        if hours_elapsed and hours_elapsed > 0.05:
            if baseline.get("volume_24h_usd") is not None and data.get("volume_24h_usd") is not None:
                trend["volume_growth_usd_per_hour"] = (
                    data["volume_24h_usd"] - baseline["volume_24h_usd"]
                ) / hours_elapsed
        if baseline.get("spread_bps") is not None and data.get("spread_bps") is not None:
            trend["spread_bps_delta"] = data["spread_bps"] - baseline["spread_bps"]

    snapshots.append({
        "ts": now_iso,
        "volume_24h_usd": data.get("volume_24h_usd"),
        "spread_bps": data.get("spread_bps"),
    })
    entry["snapshots"] = snapshots[-MAX_HISTORY_SNAPSHOTS_PER_MINT:]
    return trend


def evaluate_candidate(pair: str, data: dict, args, mcap_usd: float | None = None) -> dict:
    """Everything computable from Kraken's own AssetPairs+Ticker data --
    no secondary per-candidate lookup needed (contrast the old pipeline's
    RugCheck stage). mcap_usd is looked up in bulk by the caller (see
    main()) and passed in, defaulting to 0 (-> "emerging" tier) for any
    pair whose base asset isn't in KRAKEN_TO_COINGECKO_ID."""
    reasons_fail: list[str] = []
    symbol = data.get("altname") or pair
    volume_24h_usd = data.get("volume_24h_usd") or 0
    spread_bps = data.get("spread_bps")
    price = data.get("price_usd") or 0
    mcap_usd = mcap_usd or 0

    if data.get("status") != "online":
        reasons_fail.append(f"pair status is {data.get('status')!r}, not 'online' -- not currently tradable")
    if price <= 0:
        reasons_fail.append("no valid price reported")
    if volume_24h_usd < args.min_24h_volume_usd:
        reasons_fail.append(f"24h volume ${volume_24h_usd:,.0f} < min ${args.min_24h_volume_usd:,.0f}")
    if spread_bps is None:
        reasons_fail.append("no bid/ask spread reported -- can't confirm exit liquidity")
    elif spread_bps > args.max_spread_bps:
        reasons_fail.append(f"spread {spread_bps:.1f}bps > max {args.max_spread_bps:.1f}bps")

    tier = classify_tier(mcap_usd, volume_24h_usd, args) if not reasons_fail else None

    return {
        "pair": pair,
        "symbol": symbol,
        "eligible": len(reasons_fail) == 0,
        "tier": tier,
        "reasons_fail": reasons_fail,
        "data": {
            "price_usd": price,
            "volume_24h_usd": volume_24h_usd,
            "spread_bps": spread_bps,
            "mcap_usd": mcap_usd,
            "status": data.get("status"),
            "base": data.get("base"),
            "wsname": data.get("wsname"),
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-candidates", type=int, default=700,
                     help="Cap on candidates evaluated per run, ranked by 24h USD volume (highest first) -- set "
                          "comfortably above Kraken's current USD-pair count (~637 as of writing) so this is a "
                          "safety ceiling, not an active truncation: gather_candidates() already fetches Ticker "
                          "data for every online USD pair in one batched call regardless of this value, so "
                          "raising it costs nothing extra. Unlike the old Solana pipeline, Kraken's full universe "
                          "is cheap enough (two bulk public API calls total, no per-candidate secondary lookup) "
                          "to evaluate in full every cycle -- no rotation/sampling needed.")
    ap.add_argument("--min-24h-volume-usd", type=float, default=1_000_000,
                     help="Exit-liquidity proxy -- replaces the old pipeline's on-chain liquidity-pool check. $1M "
                          "24h volume is a real but not overly strict floor for a centralized exchange pair; "
                          "revisit with real data the same way the old min_liquidity_usd was tuned (see "
                          "config/discovery.yaml's comment history) once live/paper results exist.")
    ap.add_argument("--max-spread-bps", type=float, default=50.0,
                     help="Max bid/ask spread as a fraction of mid price, in basis points. 50bps (0.5%%) is tight "
                          "enough to exclude the most illiquid tail of Kraken's USD pairs without being so strict "
                          "it only allows the handful of most-traded majors.")
    ap.add_argument("--blue-chip-mcap-usd", type=float, default=50_000_000_000,
                     help="Market cap threshold for blue_chip tier -- much higher than the old Solana pipeline's "
                          "$50M, since Kraken's universe includes real large-cap assets (BTC, ETH) that a "
                          "Solana-memecoin-shaped threshold would badly under-classify.")
    ap.add_argument("--blue-chip-volume-usd", type=float, default=100_000_000)
    ap.add_argument("--established-mcap-usd", type=float, default=1_000_000_000)
    ap.add_argument("--established-volume-usd", type=float, default=10_000_000)
    args = ap.parse_args()

    print("Gathering USD pairs from Kraken (AssetPairs + Ticker)...")
    candidates = gather_candidates(args)
    all_pairs = sorted(candidates, key=lambda p: candidates[p]["volume_24h_usd"], reverse=True)
    pairs = all_pairs[: args.max_candidates]
    print(f"{len(candidates)} online USD pairs found; evaluating {len(pairs)} (--max-candidates, "
          f"highest 24h volume first).\n")

    market_caps = market_caps_for_pairs(candidates, pairs)

    history = load_discovery_history()
    eligible, rejected = [], []
    for i, pair in enumerate(pairs, 1):
        data = candidates[pair]
        mcap_usd = market_caps.get(pair, 0)
        print(f"  [{i}/{len(pairs)}] {data.get('altname', pair):>12}  {pair}")
        result = evaluate_candidate(pair, data, args, mcap_usd=mcap_usd)
        result["data"]["trend"] = record_and_compute_trend(pair, result["symbol"], result["data"], history)
        (eligible if result["eligible"] else rejected).append(result)
    save_discovery_history(history)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"discovery_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps({"eligible": eligible, "rejected": rejected}, indent=2))

    print(f"\n{'=' * 60}\nELIGIBLE ({len(eligible)}):")
    for r in eligible:
        d = r["data"]
        print(f"  {r['symbol']:>10}  tier={r['tier']:<10} vol24h=${d['volume_24h_usd']:>14,.0f}  "
              f"spread={d['spread_bps']:.1f}bps  mcap=${d['mcap_usd']:>14,.0f}  pair={r['pair']}")

    print(f"\nREJECTED ({len(rejected)}):")
    for r in rejected:
        print(f"  {r['symbol']:>10}  {'; '.join(r['reasons_fail'][:2])}")

    print(f"\nFull report (all fields, all reasons) saved to {out_path}")
    print("\nEligible pairs are candidates for the trade-cycle skill's signal step, not")
    print("automatic buys -- position sizing/risk checks in config/risk.yaml still apply.")


if __name__ == "__main__":
    main()
