#!/usr/bin/env python3
"""Deterministic fee calculation: what will this trade actually cost, at the
account's real current Kraken fee tier -- not a static assumption.

config/risk.yaml's max_taker_fee_bps is a conservative fallback/sanity
ceiling, not the number to actually size trades against. Kraken's fee tier
depends on live 30-day volume (or assets-on-platform, whichever is better --
see docs/STRATEGY.md "Fee-aware execution"), so it can improve over time and
the account should get the benefit of that rather than always assuming the
worst tier. Pull the real tier via the Kraken MCP `volume` tool (wraps the
private TradeVolume endpoint) each cycle (or reuse a recent cached read --
this doesn't change intraday) and parse it with parse_fee_tier() below.

Usage as a library (the primary, live-trading path):
    from account.fees import parse_fee_tier, round_trip_cost_usd, edge_clears_costs
    tier = parse_fee_tier(trade_volume_result, pair="XBTUSD",
                           default_taker_fee_bps=40, default_maker_fee_bps=25)
    check = edge_clears_costs(size_usd=145, take_profit_pct=0.35,
                               entry_fee_bps=tier.maker_fee_bps, exit_fee_bps=tier.taker_fee_bps,
                               min_edge_multiple=2.0)

Usage as a CLI (manual/local convenience only -- see kraken_common.run_kraken_cli's
docstring caveat about kraken-cli output-shape verification):
    python3 account/fees.py --pair XBTUSD    # or: python3 -m account.fees --pair XBTUSD
"""
from __future__ import annotations
import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # allows `python3 account/fees.py` directly, not just `-m account.fees`
from account.kraken_common import run_kraken_cli  # noqa: E402

# Kraken's TradeVolume endpoint reports fees as percent strings, e.g.
# "0.2600" == 0.26% == 26 basis points. 1% = 100bps, so bps = pct * 100.
BPS_PER_PERCENT = 100.0


@dataclass
class FeeTier:
    pair: str
    taker_fee_bps: float
    maker_fee_bps: float
    used_default: bool          # True if the pair wasn't found in the response and a fallback was used
    volume_30d_usd: float | None = None
    next_tier_volume_usd: float | None = None
    next_tier_taker_fee_bps: float | None = None


def _pct_str_to_bps(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value) * BPS_PER_PERCENT
    except (TypeError, ValueError):
        return None


def parse_fee_tier(trade_volume_result: dict, pair: str, default_taker_fee_bps: float, default_maker_fee_bps: float) -> FeeTier:
    """Parses Kraken's private TradeVolume endpoint response shape:
        {"currency": "ZUSD", "volume": "...",
         "fees": {"<pair>": {"fee": "0.2600", "minfee": ..., "maxfee": ...,
                              "nextfee": "0.2400", "nextvolume": "250000.0000", "tiervolume": "0.0000"}},
         "fees_maker": {"<pair>": {"fee": "0.1600", ...}}}
    (also what the Kraken MCP `volume` tool / `kraken volume --pair <pair> -o json`
    is expected to wrap -- if your installed kraken-cli reshapes field
    names, adapt this function's lookups rather than assuming it parses
    unmodified; spot-check once against a real call).

    Falls back to the given defaults (config/risk.yaml's max_taker_fee_bps
    and an assumed maker rate) if the pair isn't present in the response --
    e.g. an account that's never traded that pair before -- so a lookup
    miss degrades to a conservative estimate instead of crashing sizing
    math. Check `used_default` if the caller needs to know which happened."""
    result = trade_volume_result.get("result", trade_volume_result) if isinstance(trade_volume_result, dict) else {}
    taker = (result.get("fees") or {}).get(pair) or {}
    maker = (result.get("fees_maker") or {}).get(pair) or {}

    taker_bps = _pct_str_to_bps(taker.get("fee"))
    maker_bps = _pct_str_to_bps(maker.get("fee"))
    used_default = taker_bps is None and maker_bps is None

    volume_30d = None
    try:
        volume_30d = float(result["volume"]) if result.get("volume") not in (None, "") else None
    except (TypeError, ValueError):
        volume_30d = None

    return FeeTier(
        pair=pair,
        taker_fee_bps=taker_bps if taker_bps is not None else default_taker_fee_bps,
        maker_fee_bps=maker_bps if maker_bps is not None else default_maker_fee_bps,
        used_default=used_default,
        volume_30d_usd=volume_30d,
        next_tier_volume_usd=_to_float_or_none(taker.get("nextvolume")),
        next_tier_taker_fee_bps=_pct_str_to_bps(taker.get("nextfee")),
    )


def _to_float_or_none(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def round_trip_cost_usd(size_usd: float, entry_fee_bps: float, exit_fee_bps: float, entry_spread_bps: float = 0.0) -> float:
    """Total $ cost of opening AND closing a position of this size: both
    legs' fees, plus (optionally) the cost of crossing the spread on entry
    if it's a taker/market order. A maker/limit entry that fills at your
    quoted price has ~0 spread cost on that leg -- pass entry_spread_bps=0
    for a maker-order estimate (the default)."""
    return size_usd * (entry_fee_bps + exit_fee_bps + entry_spread_bps) / 10_000


def compare_maker_vs_taker(size_usd: float, maker_fee_bps: float, taker_fee_bps: float, spread_bps: float = 0.0) -> dict:
    """The concrete $ case for config/risk.yaml's prefer_maker_orders --
    see docs/STRATEGY.md 'Fee-aware execution'."""
    maker_cost = round_trip_cost_usd(size_usd, maker_fee_bps, maker_fee_bps, entry_spread_bps=0.0)
    taker_cost = round_trip_cost_usd(size_usd, taker_fee_bps, taker_fee_bps, entry_spread_bps=spread_bps)
    return {
        "maker_round_trip_cost_usd": maker_cost,
        "taker_round_trip_cost_usd": taker_cost,
        "savings_usd_from_preferring_maker": taker_cost - maker_cost,
    }


def edge_clears_costs(size_usd: float, take_profit_pct: float, entry_fee_bps: float, exit_fee_bps: float,
                       entry_spread_bps: float = 0.0, min_edge_multiple: float = 2.0) -> dict:
    """config/risk.yaml's min_edge_to_cost_multiple check: IF this position
    actually hits its take_profit_pct target, is the resulting dollar
    profit still comfortably (>= min_edge_multiple x) bigger than what both
    legs' fees (+ entry spread, if a taker order) cost? A trade that only
    clears its own costs by a hair isn't worth the tail risk of a
    worse-than-modeled fill eating the rest.

    Deliberately does NOT check stop_loss_pct -- that's a risk limit, not a
    profit target, and is allowed to be a somewhat-larger-than-nominal loss
    net of fees by design; gating entries on the stop-loss side would just
    make the agent reluctant to ever use a tight stop, which is backwards."""
    cost = round_trip_cost_usd(size_usd, entry_fee_bps, exit_fee_bps, entry_spread_bps)
    gross_profit_at_target = size_usd * take_profit_pct
    net_profit_at_target = gross_profit_at_target - cost
    multiple = (gross_profit_at_target / cost) if cost > 0 else float("inf")
    return {
        "round_trip_cost_usd": cost,
        "gross_profit_at_take_profit_usd": gross_profit_at_target,
        "net_profit_at_take_profit_usd": net_profit_at_target,
        "edge_to_cost_multiple": multiple,
        "clears": multiple >= min_edge_multiple,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pair", required=True, help="Kraken pair altname, e.g. XBTUSD")
    ap.add_argument("--default-taker-fee-bps", type=float, default=40.0)
    ap.add_argument("--default-maker-fee-bps", type=float, default=25.0)
    ap.add_argument("--size-usd", type=float, default=None, help="If given, also print a round-trip cost estimate for this size")
    args = ap.parse_args()

    try:
        payload = run_kraken_cli(["volume", "--pair", args.pair])
    except RuntimeError as e:
        print(f"! {e}", file=sys.stderr)
        sys.exit(1)

    tier = parse_fee_tier(payload, args.pair, args.default_taker_fee_bps, args.default_maker_fee_bps)
    out = {
        "pair": tier.pair, "taker_fee_bps": tier.taker_fee_bps, "maker_fee_bps": tier.maker_fee_bps,
        "used_default": tier.used_default, "volume_30d_usd": tier.volume_30d_usd,
        "next_tier_volume_usd": tier.next_tier_volume_usd, "next_tier_taker_fee_bps": tier.next_tier_taker_fee_bps,
    }
    if args.size_usd is not None:
        out["round_trip_cost_usd_at_size"] = compare_maker_vs_taker(args.size_usd, tier.maker_fee_bps, tier.taker_fee_bps)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
