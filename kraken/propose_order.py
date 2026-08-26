#!/usr/bin/env python3
"""Kraken order proposal/execution CLI. Defaults to quote-and-validate-only
(no funds move, nothing sent to Kraken's order book) -- pass --execute to
actually place the order. This mirrors wallet/swap.mjs's contract exactly,
now retired in favor of this script (see wallet/DEPRECATED.md).

IMPORTANT: an AI agent (Claude or otherwise) must never pass --execute on
your behalf. Placing a real order is something you run yourself,
deliberately, in your own terminal -- see .claude/skills/trade-cycle/SKILL.md
step 8 and CLAUDE.md rule 1. Without --execute, this NEVER places or
attempts to place a real order -- the quote itself only reads Kraken's
public Ticker endpoint (no API key needed at all). If KRAKEN_API_KEY/
KRAKEN_API_SECRET are configured, it additionally runs Kraken's own
`validate: true` dry-run check on the AddOrder endpoint (still zero funds
movement -- Kraken's server-side confirmation that the order would be
accepted); if no key is configured, that extra check is simply skipped and
reported as such, not treated as an error.

Usage:
    python3 kraken/propose_order.py XBTUSD buy 25.00              # quote only: buy $25 of XBTUSD at market
    python3 kraken/propose_order.py XBTUSD buy 25.00 --execute     # places the real order
    python3 kraken/propose_order.py XBTUSD sell 0.0004 --units base --execute
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kraken import client as kc  # noqa: E402
from kraken import precision as kp  # noqa: E402


def _mid_price(t: dict) -> float:
    ask = float(t["a"][0])
    bid = float(t["b"][0])
    return (ask + bid) / 2


def _spread_bps(t: dict) -> float:
    ask = float(t["a"][0])
    bid = float(t["b"][0])
    mid = (ask + bid) / 2
    return ((ask - bid) / mid) * 10_000 if mid else 0.0


def build_proposal(pair: str, side: str, amount: float, units: str) -> dict:
    tick = kc.ticker([pair])
    key = next(iter(tick), None)
    if key is None:
        raise SystemExit(f"Kraken returned no ticker data for pair {pair!r} -- check the pair name (e.g. XBTUSD, ETHUSD)")
    t = tick[key]
    price = _mid_price(t)
    spread_bps = _spread_bps(t)
    volume = amount if units == "base" else amount / price
    usd_value = volume * price

    # Kraken rejects an order whose price/volume carries more decimal
    # places than the pair allows, and separately rejects one below the
    # pair's own ordermin/costmin -- both real, per-pair constraints (not
    # config/risk.yaml's project-level min_trade_usd, which can be smaller
    # than what Kraken itself requires for a thin/expensive pair). Checked
    # here so a proposal that would be rejected on these grounds says so up
    # front, before anyone runs --execute.
    pairs_info = kc.asset_pairs()
    info = pairs_info.get(key, {})
    volume = kp.round_volume(volume, info.get("lot_decimals"))
    price_rounded = kp.round_price(price, info.get("pair_decimals"))
    usd_value = volume * price
    minimum_check = kp.clamp_to_pair_minimums(usd_value, price, info.get("ordermin"), info.get("costmin"))

    return {
        "pair": pair, "side": side, "ordertype": "market",
        "volume": volume, "estimated_price_usd": price_rounded,
        "estimated_usd_value": round(usd_value, 2), "spread_bps": round(spread_bps, 2),
        "meets_pair_minimums": minimum_check["ok"], "pair_minimum_reason": minimum_check["reason"],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pair", help="Kraken pair, e.g. XBTUSD, ETHUSD, SOLUSD")
    ap.add_argument("side", choices=["buy", "sell"])
    ap.add_argument("amount", type=float, help="Amount to trade, in --units (default: usd)")
    ap.add_argument("--units", choices=["usd", "base"], default="usd",
                     help="Interpret `amount` as USD notional (default) or as a quantity of the base asset")
    ap.add_argument("--execute", action="store_true",
                     help="Actually place the order. NEVER pass this from an AI agent session -- see module docstring.")
    args = ap.parse_args()

    proposal = build_proposal(args.pair, args.side, args.amount, args.units)
    print(f"Quote: {args.side} {proposal['volume']} {args.pair} (~${proposal['estimated_usd_value']:.2f}) "
          f"@ ~${proposal['estimated_price_usd']:.6f}, spread {proposal['spread_bps']:.1f}bps", file=sys.stderr)
    if not proposal["meets_pair_minimums"]:
        print(f"! {proposal['pair_minimum_reason']} -- Kraken will reject this order as sized", file=sys.stderr)

    if not args.execute:
        # Kraken's own validate=True flag double-checks the order would be
        # accepted (balance, minimums, pair status) without ever placing it --
        # still zero funds movement, just a stronger dry-run than the ticker
        # quote alone. Requires KRAKEN_API_KEY/SECRET (it's a private
        # endpoint even in validate mode) -- gracefully skipped, not an
        # error, if no key is configured yet.
        import os
        if not (os.environ.get("KRAKEN_API_KEY") and os.environ.get("KRAKEN_API_SECRET")):
            print(json.dumps({"mode": "quote-only", "executed": False, "proposal": proposal,
                               "validate_result": "skipped -- KRAKEN_API_KEY/SECRET not configured"}, indent=2))
            return
        try:
            validation = kc.add_order(args.pair, args.side, "market", str(proposal["volume"]), validate=True)
        except kc.KrakenAPIError as e:
            print(json.dumps({"mode": "quote-only", "executed": False, "proposal": proposal, "validate_error": str(e)}, indent=2))
            return
        print(json.dumps({"mode": "quote-only", "executed": False, "proposal": proposal, "validate_result": validation}, indent=2))
        return

    # Everything below here places a real order.
    result = kc.add_order(args.pair, args.side, "market", str(proposal["volume"]), validate=False)
    print(json.dumps({"mode": "executed", "executed": True, "proposal": proposal, "result": result}, indent=2))


if __name__ == "__main__":
    main()
