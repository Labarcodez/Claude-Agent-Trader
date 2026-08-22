#!/usr/bin/env python3
"""Dynamic Solana token discovery + automated safety scoring, stdlib only.

Replaces a hand-maintained watchlist: pulls live candidates from Jupiter's
Tokens API v2 (real trading interest, trending, newest pools), which already
returns per-token on-chain audit data (mint/freeze authority, top-holder
concentration, an "organic score" that filters out wash-traded/bot volume,
and first-pool creation time for age) at zero extra cost -- then
cross-checks survivors against RugCheck.xyz for one thing Jupiter's data
doesn't cover: whether the mint has already been confirmed as a rug pull.

All data sources are free, public, and require no API key -- but they're
someone else's infrastructure with real rate limits, so this script is
deliberately conservative about request volume: RugCheck is only called for
candidates that already pass every check computed from Jupiter's own
response, and everything backs off and retries once on a 429 rather than
hammering the endpoint.

See config/discovery.yaml for the human-readable version of these
thresholds (keep the two in sync by hand -- this script does not parse that
file, to stay dependency-free) and docs/STRATEGY.md "Autonomous discovery"
for the reasoning and the empirical notes on what these APIs actually
return (some fields are less trustworthy than they look -- documented
there).

Usage:
    python3 research/discover_candidates.py
    python3 research/discover_candidates.py --max-candidates 15 --min-liquidity-usd 300000
"""
from __future__ import annotations
import argparse
import json
import random
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# Discovered token symbols can contain arbitrary Unicode (scam/spam tokens routinely
# use lookalike characters); Windows consoles default to a narrow codepage (cp1252)
# that can't encode most of it, which otherwise crashes this script mid-run -- after
# the expensive discovery/RugCheck work is already done -- on nothing but a print().
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

JUPITER_BASE = "https://api.jup.ag/tokens/v2"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"
USER_AGENT = "claude-agent-trader-discovery/1.0"

RESULTS_DIR = Path(__file__).parent / "results"

# Native SOL + Circle's Solana USDC -- settlement/base assets, not discovery
# candidates. Kept here (not in discovery output) so they're never
# accidentally excluded *or* re-evaluated as if they needed due diligence.
CORE_ASSET_MINTS = {
    "So11111111111111111111111111111111111111112",  # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
}


# 429 (rate limited) and 502/503/504 (bad gateway/unavailable/gateway
# timeout) are all transient, worth retrying -- unlike a definitive client
# error (400/401/403/404), which retrying can't fix. Observed live:
# RugCheck.xyz returned 502 for two otherwise-clean candidates in the same
# cycle; without a retry here, evaluate_candidate()'s fail-safe design
# correctly rejects them rather than trading unconfirmed -- but "reject a
# legitimate candidate over one brief upstream hiccup" is a worse outcome
# than "retry a couple times first" when the fix is this cheap.
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
            # Broad on purpose: URLError and TimeoutError are both OSError
            # subclasses (HTTPError too, but it's caught by the more specific
            # clause above first), so this also catches raw connection-level
            # failures -- confirmed live when RugCheck.xyz reset the
            # connection mid-request and this environment's urllib raised a
            # bare http.client.RemoteDisconnected instead of wrapping it in
            # URLError. Un-widened, that crashed the entire cycle with no
            # journal entry instead of retrying like every other transient
            # failure here.
            last_err = e
            time.sleep(backoff)
    print(f"  ! request failed: {url} ({last_err})", file=sys.stderr)
    return None


def fetch_jupiter_category(category: str, interval: str, limit: int) -> list[dict]:
    return _get_json(f"{JUPITER_BASE}/{category}/{interval}?limit={limit}") or []


def fetch_jupiter_recent(limit: int) -> list[dict]:
    return _get_json(f"{JUPITER_BASE}/recent?limit={limit}") or []


def fetch_jupiter_tag(tag: str) -> list[dict]:
    """Verified live: query=verified alone returns 2,561 tokens -- Jupiter's
    full verified-token list, not a momentum snapshot like the toporganicscore/
    toptrending/recent sources above. Those three only ever surface whatever
    happens to be trending/organic/newest *right now*, so a legitimate,
    established-but-not-currently-hot token could never be discovered at all
    regardless of how many cycles run. This is the actual breadth fix -- the
    max_candidates rotation (see paper_trading/run_paper_cycle.py) is what
    makes evaluating a pool this size safe without hammering RugCheck."""
    return _get_json(f"{JUPITER_BASE}/tag?query={tag}") or []


def fetch_rugcheck_report(mint: str) -> dict | None:
    return _get_json(f"{RUGCHECK_BASE}/tokens/{mint}/report")


def fetch_dexscreener_best_solana_pair(mint: str) -> dict | None:
    data = _get_json(f"{DEXSCREENER_BASE}/tokens/{mint}")
    pairs = [p for p in (data or {}).get("pairs") or [] if p.get("chainId") == "solana"]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)


def gather_candidates(args) -> dict[str, dict]:
    """Returns {mint: jupiter_token_data}, deduped across sources, core assets excluded."""
    candidates: dict[str, dict] = {}
    # 8 back-to-back Jupiter calls (2 organic + 2 trending + 2 traded + 1
    # recent + 1 verified) with no spacing was hitting 429s on 2 of them most
    # cycles -- always recovered by _get_json()'s own retry, but at a real
    # cost (two live cycles took ~100s+ each, mostly retry backoff, vs the
    # usual ~12-15s). A brief pause between calls spreads the load instead of
    # bursting it, the same fix that already worked for the pricing call in
    # paper_trading/run_paper_cycle.py.
    first_call = True

    def _paced_call(fn, *fn_args):
        nonlocal first_call
        if not first_call:
            time.sleep(0.5)
        first_call = False
        return fn(*fn_args)

    if not args.no_organic:
        for interval in ("6h", "24h"):
            for tok in _paced_call(fetch_jupiter_category, "toporganicscore", interval, args.limit_per_source):
                candidates.setdefault(tok["id"], tok)

    if not args.no_trending:
        for interval in ("1h", "6h"):
            for tok in _paced_call(fetch_jupiter_category, "toptrending", interval, args.limit_per_source):
                candidates.setdefault(tok["id"], tok)

    if not args.no_traded:
        for interval in ("6h", "24h"):
            for tok in _paced_call(fetch_jupiter_category, "toptraded", interval, args.limit_per_source):
                candidates.setdefault(tok["id"], tok)

    if not args.no_recent:
        for tok in _paced_call(fetch_jupiter_recent, args.limit_per_source):
            candidates.setdefault(tok["id"], tok)

    if not args.no_verified:
        for tok in _paced_call(fetch_jupiter_tag, "verified"):
            candidates.setdefault(tok["id"], tok)

    for mint in CORE_ASSET_MINTS:
        candidates.pop(mint, None)

    return candidates


def _pool_age_hours(first_pool: dict | None) -> float | None:
    if not first_pool or not first_pool.get("createdAt"):
        return None
    try:
        created = datetime.fromisoformat(str(first_pool["createdAt"]).replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        # Fail-safe, not fail-crash: a malformed/unexpected createdAt shape from
        # Jupiter's API (e.g. a non-string value) must not take down the whole
        # discovery run over one candidate -- treat it the same as "no age
        # reported" (evaluate_candidate() then rejects that candidate on the
        # "can't confirm token age" reason, same as a genuinely missing field).
        return None
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600


def classify_tier(mcap: float, holder_count: int, is_verified: bool, args) -> str:
    if mcap >= args.blue_chip_mcap_usd and holder_count >= args.blue_chip_holder_count and is_verified:
        return "blue_chip"
    if mcap >= args.established_mcap_usd and holder_count >= args.established_holder_count:
        return "established"
    return "emerging"  # includes memecoins and other newer/smaller tokens that still pass every safety check


def evaluate_candidate(mint: str, tok: dict, args) -> dict:
    """Two-stage check: (1) everything computable from Jupiter's own response
    for free, (2) RugCheck's `rugged` flag -- but only spent on candidates
    that already survive stage 1, to keep request volume down."""
    reasons_fail: list[str] = []
    symbol = tok.get("symbol", "?")
    liquidity = tok.get("liquidity") or 0
    holders = tok.get("holderCount") or 0
    # Real circulating mcap only -- NOT fdv. FDV (fully diluted valuation)
    # counts locked/unvested/unminted supply as if it were already circulating,
    # so a low-float token can show a huge fdv while its real, tradeable market
    # cap is tiny. Treating fdv as a mcap stand-in would let such a token
    # falsely qualify for "established"/"blue_chip" tier sizing (a bigger
    # position multiplier) when it's actually "emerging"-risk. Missing/zero
    # mcap defaults to 0, which safely falls through to the "emerging" tier
    # in classify_tier() below rather than overstating it.
    mcap = tok.get("mcap") or 0
    fdv = tok.get("fdv") or 0
    audit = tok.get("audit") or {}
    organic_score = tok.get("organicScore")
    top_holder_pct = audit.get("topHoldersPercentage")
    pool_age_hours = _pool_age_hours(tok.get("firstPool"))

    if liquidity < args.min_liquidity_usd:
        reasons_fail.append(f"liquidity ${liquidity:,.0f} < min ${args.min_liquidity_usd:,.0f}")
    if holders < args.min_holder_count:
        reasons_fail.append(f"holderCount {holders} < min {args.min_holder_count}")
    if organic_score is None:
        reasons_fail.append("no organicScore reported -- can't confirm real (non-wash-traded) demand")
    elif organic_score < args.min_organic_score:
        reasons_fail.append(f"organicScore {organic_score:.1f} < min {args.min_organic_score}")
    if args.require_mint_renounced and not audit.get("mintAuthorityDisabled"):
        reasons_fail.append("mint authority not disabled (deployer can still mint new supply)")
    if args.require_freeze_renounced and not audit.get("freezeAuthorityDisabled"):
        reasons_fail.append("freeze authority not disabled (deployer can still freeze holder accounts)")
    if top_holder_pct is not None and top_holder_pct > args.max_top_holder_pct:
        reasons_fail.append(f"top holder owns {top_holder_pct:.1f}% of supply > max {args.max_top_holder_pct}%")
    if pool_age_hours is None:
        reasons_fail.append("no first-pool creation time reported -- can't confirm token age")
    elif pool_age_hours < args.min_pool_age_hours:
        reasons_fail.append(f"pool age {pool_age_hours:.1f}h < min {args.min_pool_age_hours}h")

    rug_rugged = None
    rug_risks: list[str] = []
    if not reasons_fail or args.always_rugcheck:
        time.sleep(args.request_delay)
        rug = fetch_rugcheck_report(mint)
        if rug is None:
            reasons_fail.append("RugCheck unavailable -- fail-safe reject rather than trade unconfirmed")
        else:
            rug_rugged = rug.get("rugged")
            if rug_rugged:
                reasons_fail.append("RugCheck flags this mint as an already-confirmed rug")
            for r in rug.get("risks") or []:
                if isinstance(r, dict) and str(r.get("level", "")).lower() in ("danger", "critical", "high"):
                    name = r.get("name") or r.get("description") or str(r)
                    rug_risks.append(name)
                    reasons_fail.append(f"RugCheck risk flag: {name}")

    dex_pair = None
    if args.cross_check_dexscreener and not reasons_fail:
        time.sleep(args.request_delay)
        dex_pair = fetch_dexscreener_best_solana_pair(mint)
        if dex_pair:
            dex_liq = (dex_pair.get("liquidity") or {}).get("usd", 0) or 0
            if dex_liq < args.min_liquidity_usd * 0.5:  # allow some divergence between sources before treating it as a red flag
                reasons_fail.append(f"DexScreener liquidity ${dex_liq:,.0f} diverges sharply below Jupiter's ${liquidity:,.0f}")

    is_verified = bool(tok.get("isVerified")) and audit.get("mintAuthorityDisabled") and audit.get("freezeAuthorityDisabled")
    tier = classify_tier(mcap, holders, is_verified, args) if not reasons_fail else None

    return {
        "mint": mint,
        "symbol": symbol,
        "eligible": len(reasons_fail) == 0,
        "tier": tier,
        "reasons_fail": reasons_fail,
        "data": {
            "liquidity_usd": liquidity,
            "holder_count": holders,
            "mcap_usd": mcap,
            "fdv_usd": fdv,   # reported for context only -- never used for tier classification, see mcap comment above
            "organic_score": organic_score,
            "organic_score_label": tok.get("organicScoreLabel"),
            "top_holder_pct": top_holder_pct,
            "pool_age_hours": pool_age_hours,
            "mint_authority_disabled": audit.get("mintAuthorityDisabled"),
            "freeze_authority_disabled": audit.get("freezeAuthorityDisabled"),
            "jupiter_is_verified": tok.get("isVerified"),
            "tags": tok.get("tags"),
            "rugcheck_rugged": rug_rugged,
            "rugcheck_risk_flags": rug_risks,
            "dexscreener_cross_checked": dex_pair is not None,
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-candidates", type=int, default=250,
                     help="Cap on unique candidates evaluated per run (be a good citizen to free APIs) -- keep in "
                          "sync with config/discovery.yaml's max_candidates_per_cycle. Was 40; raised after "
                          "verifying live that a 250-sample run found a real eligible candidate (MANLET) on the "
                          "same safety thresholds a same-day 40-sample run had missed by chance -- see "
                          "config/discovery.yaml's comment on max_candidates_per_cycle for the full reasoning.")
    ap.add_argument("--limit-per-source", type=int, default=15)
    ap.add_argument("--request-delay", type=float, default=0.4, help="Seconds between RugCheck/DexScreener calls")
    ap.add_argument("--no-organic", action="store_true", help="Skip the toporganicscore source")
    ap.add_argument("--no-trending", action="store_true", help="Skip the toptrending source")
    ap.add_argument("--no-traded", action="store_true", help="Skip the toptraded (by volume) source")
    ap.add_argument("--no-recent", action="store_true", help="Skip the recent-pools source")
    ap.add_argument("--no-verified", action="store_true",
                     help="Skip Jupiter's full verified-token list (2500+ tokens as of writing) -- the actual "
                          "ecosystem-breadth source; the other sources are all momentum snapshots (whatever's "
                          "trending/organic/newest right now) that can never surface an established-but-not-"
                          "currently-hot token no matter how many cycles run")
    ap.add_argument("--always-rugcheck", action="store_true",
                     help="Call RugCheck even for candidates that already fail Jupiter's checks (uses more requests)")
    ap.add_argument("--cross-check-dexscreener", dest="cross_check_dexscreener", action="store_true", default=True)
    ap.add_argument("--no-cross-check-dexscreener", dest="cross_check_dexscreener", action="store_false")
    # Safety thresholds -- keep in sync with config/discovery.yaml's `safety` block
    ap.add_argument("--min-liquidity-usd", type=float, default=250_000)
    ap.add_argument("--min-holder-count", type=int, default=500)
    ap.add_argument("--min-pool-age-hours", type=float, default=72)
    ap.add_argument("--min-organic-score", type=float, default=40)
    ap.add_argument("--max-top-holder-pct", type=float, default=22.0)
    ap.add_argument("--require-mint-renounced", dest="require_mint_renounced", action="store_true", default=True)
    ap.add_argument("--allow-mint-authority", dest="require_mint_renounced", action="store_false")
    ap.add_argument("--require-freeze-renounced", dest="require_freeze_renounced", action="store_true", default=True)
    ap.add_argument("--allow-freeze-authority", dest="require_freeze_renounced", action="store_false")
    # Tier thresholds -- keep in sync with config/discovery.yaml's `tiers` block
    ap.add_argument("--blue-chip-mcap-usd", type=float, default=50_000_000)
    ap.add_argument("--blue-chip-holder-count", type=int, default=10_000)
    ap.add_argument("--established-mcap-usd", type=float, default=5_000_000)
    ap.add_argument("--established-holder-count", type=int, default=2_000)
    args = ap.parse_args()

    print("Gathering candidates from Jupiter Tokens API v2...")
    candidates = gather_candidates(args)
    # Shuffle before truncating -- gather_candidates() lists momentum sources
    # (organic/trending/traded/recent, ~70-130 tokens) before the
    # verified-tag source (~2,561 tokens); without shuffling first, a small
    # momentum pool can supply max_candidates' worth of tokens on its own
    # every run, so this (what trade-cycle actually runs for live decisions)
    # would never evaluate anything past roughly the first ~130 of a
    # 2,600-token pool -- see the same fix in
    # paper_trading/run_paper_cycle.py's select_candidates_for_rotation()
    # for the live-verified version of this bug.
    all_mints = list(candidates.keys())
    random.shuffle(all_mints)
    mints = all_mints[: args.max_candidates]
    print(f"{len(candidates)} unique candidates found (deduped across sources); evaluating {len(mints)} (--max-candidates, sampled across the full pool).\n")

    eligible, rejected = [], []
    for i, mint in enumerate(mints, 1):
        tok = candidates[mint]
        print(f"  [{i}/{len(mints)}] {tok.get('symbol', '?'):>10}  {mint}")
        result = evaluate_candidate(mint, tok, args)
        (eligible if result["eligible"] else rejected).append(result)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"discovery_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps({"eligible": eligible, "rejected": rejected}, indent=2))

    print(f"\n{'=' * 60}\nELIGIBLE ({len(eligible)}):")
    for r in eligible:
        d = r["data"]
        print(f"  {r['symbol']:>10}  tier={r['tier']:<10} liq=${d['liquidity_usd']:>12,.0f}  "
              f"holders={d['holder_count']:>7}  organic={d['organic_score']}  mint={r['mint']}")

    print(f"\nREJECTED ({len(rejected)}):")
    for r in rejected:
        print(f"  {r['symbol']:>10}  {'; '.join(r['reasons_fail'][:2])}")

    print(f"\nFull report (all fields, all reasons) saved to {out_path}")
    print("\nEligible tokens are candidates for the trade-cycle skill's signal step, not")
    print("automatic buys -- position sizing/risk checks in config/risk.yaml still apply,")
    print("and this script's output should still be spot-checked, not trusted blindly.")


if __name__ == "__main__":
    main()
