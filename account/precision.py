#!/usr/bin/env python3
"""Order-construction safety: round price/volume to what Kraken will
actually accept for a given pair, and check a proposed size against the
pair's own enforced minimums. Kraken rejects an order whose price or
volume carries more decimal places than the pair allows, and separately
rejects an order below the pair's `ordermin`/`costmin` -- both are
per-pair values from AssetPairs (see research/discover_candidates.py's
discovery output, which surfaces pair_decimals/lot_decimals/ordermin/costmin
for exactly this).
"""
from __future__ import annotations
import math


def round_volume(volume: float, lot_decimals: int | None) -> float:
    """Floors (never rounds up) to the pair's lot_decimals -- never claim to
    trade more base-currency volume than was actually intended/affordable.
    Rounding up even by one unit in the last decimal place could turn an
    order that fit within available cash into one that doesn't."""
    if lot_decimals is None or volume <= 0:
        return volume
    factor = 10 ** lot_decimals
    return math.floor(volume * factor) / factor


def round_price(price: float, pair_decimals: int | None) -> float:
    """Rounds to the pair's pair_decimals (nearest, not floored -- unlike
    volume, a limit price a tick off in either direction isn't a solvency
    issue, just a precision one)."""
    if pair_decimals is None:
        return price
    return round(price, pair_decimals)


def clamp_to_pair_minimums(size_usd: float, price: float, ordermin: float | None, costmin: float | None) -> dict:
    """Checks a proposed USD position size against the pair's own
    Kraken-enforced minimums, which can exceed config/risk.yaml's
    project-level min_trade_usd for a thin and/or high-priced pair (a
    costmin of a few dollars is common, but ordermin * price can push the
    effective floor higher for an expensive base asset). Returns whether
    the size already clears both floors, and if not, the minimum USD size
    that WOULD -- sizing/risk-check code decides whether bumping up to that
    (while still respecting max_position_usd etc.) is acceptable; this
    function only reports the floor, it doesn't decide the trade-off."""
    if price is None or price <= 0:
        return {"ok": False, "reason": "non-positive or missing price", "min_required_usd": None}

    min_required_usd = 0.0
    if ordermin is not None:
        try:
            min_required_usd = max(min_required_usd, float(ordermin) * price)
        except (TypeError, ValueError):
            pass
    if costmin is not None:
        try:
            min_required_usd = max(min_required_usd, float(costmin))
        except (TypeError, ValueError):
            pass

    ok = size_usd >= min_required_usd
    reason = None if ok else f"${size_usd:.2f} < pair minimum ${min_required_usd:.2f} (Kraken ordermin/costmin)"
    return {"ok": ok, "reason": reason, "min_required_usd": min_required_usd}
