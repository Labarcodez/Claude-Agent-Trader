#!/usr/bin/env python3
"""Deterministic fee calculation: what will this trade actually cost, at the
account's real current Kraken fee tier -- not a static assumption.

Kraken's fee tier depends on live 30-day volume, so it can improve over
time and sizing/edge checks should get the benefit of that rather than
always assuming the worst tier. Pull the real tier via
`kraken.client.trade_volume()` (or reuse a recent cached read -- this
doesn't change intraday) and parse it with parse_fee_tier() below.

Ported from a parallel Kraken migration's independently-written account/
package (same underlying Kraken fee semantics), adapted here to this
project's pure-Python REST client (`kraken.client.trade_volume()`) instead
of that version's kraken-cli dependency.

Usage:
    from kraken.client import trade_volume
    from kraken.fees import parse_fee_tier, edge_clears_costs
    tier = parse_fee_tier(trade_volume("XBTUSD"), pair="XBTUSD",
                           default_taker_fee_bps=40, default_maker_fee_bps=25)
    check = edge_clears_costs(size_usd=145, take_profit_pct=0.35,
                               entry_fee_bps=tier.maker_fee_bps, exit_fee_bps=tier.taker_fee_bps,
                               min_edge_multiple=2.0)
"""
from __future__ import annotations
from dataclasses import dataclass

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


def _to_float_or_none(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_fee_tier(trade_volume_result: dict, pair: str, default_taker_fee_bps: float, default_maker_fee_bps: float) -> FeeTier:
    """Parses `kraken.client.trade_volume()`'s response shape (Kraken's
    private TradeVolume endpoint):
        {"currency": "ZUSD", "volume": "...",
         "fees": {"<pair>": {"fee": "0.2600", "minfee": ..., "maxfee": ...,
                              "nextfee": "0.2400", "nextvolume": "250000.0000", "tiervolume": "0.0000"}},
         "fees_maker": {"<pair>": {"fee": "0.1600", ...}}}

    Falls back to the given defaults if the pair isn't present in the
    response -- e.g. an account that's never traded that pair before -- so
    a lookup miss degrades to a conservative estimate instead of crashing
    sizing math. Check `used_default` if the caller needs to know which
    happened."""
    result = trade_volume_result.get("result", trade_volume_result) if isinstance(trade_volume_result, dict) else {}
    taker = (result.get("fees") or {}).get(pair) or {}
    maker = (result.get("fees_maker") or {}).get(pair) or {}

    taker_bps = _pct_str_to_bps(taker.get("fee"))
    maker_bps = _pct_str_to_bps(maker.get("fee"))
    used_default = taker_bps is None and maker_bps is None

    return FeeTier(
        pair=pair,
        taker_fee_bps=taker_bps if taker_bps is not None else default_taker_fee_bps,
        maker_fee_bps=maker_bps if maker_bps is not None else default_maker_fee_bps,
        used_default=used_default,
        volume_30d_usd=_to_float_or_none(result.get("volume")),
        next_tier_volume_usd=_to_float_or_none(taker.get("nextvolume")),
        next_tier_taker_fee_bps=_pct_str_to_bps(taker.get("nextfee")),
    )


def round_trip_cost_usd(size_usd: float, entry_fee_bps: float, exit_fee_bps: float, entry_spread_bps: float = 0.0) -> float:
    """Total $ cost of opening AND closing a position of this size: both
    legs' fees, plus (optionally) the cost of crossing the spread on entry
    if it's a taker/market order. A maker/limit entry that fills at your
    quoted price has ~0 spread cost on that leg -- pass entry_spread_bps=0
    for a maker-order estimate (the default)."""
    return size_usd * (entry_fee_bps + exit_fee_bps + entry_spread_bps) / 10_000


def compare_maker_vs_taker(size_usd: float, maker_fee_bps: float, taker_fee_bps: float, spread_bps: float = 0.0) -> dict:
    """The concrete $ difference between a maker (limit) and taker (market)
    round trip at this size."""
    maker_cost = round_trip_cost_usd(size_usd, maker_fee_bps, maker_fee_bps, entry_spread_bps=0.0)
    taker_cost = round_trip_cost_usd(size_usd, taker_fee_bps, taker_fee_bps, entry_spread_bps=spread_bps)
    return {
        "maker_round_trip_cost_usd": maker_cost,
        "taker_round_trip_cost_usd": taker_cost,
        "savings_usd_from_preferring_maker": taker_cost - maker_cost,
    }


def edge_clears_costs(size_usd: float, take_profit_pct: float, entry_fee_bps: float, exit_fee_bps: float,
                       entry_spread_bps: float = 0.0, min_edge_multiple: float = 2.0) -> dict:
    """IF this position actually hits its take_profit_pct target, is the
    resulting dollar profit still comfortably (>= min_edge_multiple x)
    bigger than what both legs' fees (+ entry spread, if a taker order)
    cost? A trade that only clears its own costs by a hair isn't worth the
    tail risk of a worse-than-modeled fill eating the rest.

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
