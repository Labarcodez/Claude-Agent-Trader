#!/usr/bin/env python3
"""Deterministic portfolio valuation: "how much is actually in the account,
in USD, right now" -- computed from raw balance + price data, not estimated.

This replaces ad-hoc mental arithmetic in the trade-cycle skill: after
calling the Kraken MCP `balance` tool (and pricing tool/`ticker` for each
non-cash holding), feed the resulting dicts into normalize_balances() and
usd_value_of_balances() instead of adding the numbers up by hand. Every
downstream risk check (circuit breaker floor, portfolio heat, position
sizing, max_non_stable_exposure_fraction) depends on this number being
right -- a silent arithmetic slip here corrupts everything after it.

Usage as a library (the primary, live-trading path):
    from account.portfolio import normalize_balances, usd_value_of_balances
    balances = normalize_balances(raw_balance_dict_from_mcp_tool)
    valuation = usd_value_of_balances(balances, prices_usd_dict_from_ticker_calls)
    print(valuation.total_usd, valuation.non_stable_exposure_fraction)

Usage as a CLI (manual/local convenience only -- see kraken_common.run_kraken_cli's
docstring caveat about kraken-cli output-shape verification):
    python3 account/portfolio.py    # or: python3 -m account.portfolio
"""
from __future__ import annotations
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # allows `python3 account/portfolio.py` directly, not just `-m account.portfolio`
from account.kraken_common import STABLE_ASSETS, normalize_asset, run_kraken_cli  # noqa: E402

# Kraken's /Balance only returns nonzero balances, but a fee remnant or a
# tiny unwind can leave float noise (e.g. 1e-12) that isn't really a
# position -- filtered out here rather than left to confuse a "which
# pairs does the account actually hold" read downstream.
DUST_EPSILON = 1e-9


@dataclass
class AssetValuation:
    asset: str
    quantity: float
    price_usd: float | None
    value_usd: float


@dataclass
class PortfolioValuation:
    assets: list[AssetValuation]
    total_usd: float
    cash_usd: float
    non_cash_usd: float
    pricing_errors: list[str] = field(default_factory=list)

    @property
    def non_stable_exposure_fraction(self) -> float:
        """Directly answers config/risk.yaml's max_non_stable_exposure_fraction
        check -- 0.0 if the portfolio has no value at all (nothing to be
        "exposed" relative to), never a division error."""
        return (self.non_cash_usd / self.total_usd) if self.total_usd > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "assets": [
                {"asset": a.asset, "quantity": a.quantity, "price_usd": a.price_usd, "value_usd": a.value_usd}
                for a in self.assets
            ],
            "total_usd": self.total_usd,
            "cash_usd": self.cash_usd,
            "non_cash_usd": self.non_cash_usd,
            "non_stable_exposure_fraction": self.non_stable_exposure_fraction,
            "pricing_errors": self.pricing_errors,
        }


def normalize_balances(raw_balances: dict) -> dict[str, float]:
    """raw_balances: Kraken's private Balance endpoint shape,
    {asset_code: "amount string"} -- also what the Kraken MCP `balance`
    tool / `kraken balance -o json`'s result is expected to look like. Pass
    the dict straight through; if your tool output nests it under a
    "result" key, unwrap that before calling this (kept out of this
    function so it stays a pure {code: amount} -> {asset: quantity} map).

    Merges legacy and normalized codes for the same underlying asset (e.g.
    if a response ever mixed "XXBT" and "XBT" for the same holding) rather
    than silently only counting one."""
    out: dict[str, float] = {}
    for code, amount in (raw_balances or {}).items():
        try:
            qty = float(amount)
        except (TypeError, ValueError):
            continue
        if abs(qty) < DUST_EPSILON:
            continue
        asset = normalize_asset(code)
        out[asset] = out.get(asset, 0.0) + qty
    return out


def usd_value_of_balances(balances: dict[str, float], prices_usd: dict[str, float] | None = None) -> PortfolioValuation:
    """balances: {asset: quantity}, e.g. from normalize_balances().
    prices_usd: {asset: price_usd} for non-stable assets (from live Ticker
    calls). USD/USDT/USDC default to $1.00 UNLESS an explicit override is
    given here -- pass a real USDTUSD/USDCUSD ticker price if you want
    depeg risk reflected rather than assumed away (see
    config/core_assets.yaml's note on this).

    An asset with no known price is EXCLUDED from total_usd -- NOT counted
    as $0 silently. This is a deliberate, conservative choice: for live
    circuit-breaker math, excluding an unpriced position's value biases the
    computed total DOWN, which in the worst case causes a false-positive
    circuit-breaker trip (halts new entries) rather than a false sense of
    safety that lets trading continue past a real drawdown the agent simply
    failed to price. Any non-empty `pricing_errors` on the result is a
    reason to investigate before trusting `total_usd`, not background
    noise to ignore."""
    prices_usd = dict(prices_usd or {})
    assets: list[AssetValuation] = []
    pricing_errors: list[str] = []
    total_usd = 0.0
    cash_usd = 0.0
    non_cash_usd = 0.0

    for asset, qty in balances.items():
        if asset in prices_usd:
            price = prices_usd[asset]
        elif asset in STABLE_ASSETS:
            price = 1.0
        else:
            price = None

        if price is None:
            pricing_errors.append(f"no price available for {asset} (qty={qty}) -- excluded from total_usd")
            assets.append(AssetValuation(asset=asset, quantity=qty, price_usd=None, value_usd=0.0))
            continue

        value = qty * price
        assets.append(AssetValuation(asset=asset, quantity=qty, price_usd=price, value_usd=value))
        total_usd += value
        if asset in STABLE_ASSETS:
            cash_usd += value
        else:
            non_cash_usd += value

    return PortfolioValuation(
        assets=assets, total_usd=total_usd, cash_usd=cash_usd,
        non_cash_usd=non_cash_usd, pricing_errors=pricing_errors,
    )


def _infer_usd_pair(asset: str) -> str:
    return f"{asset}USD"


def _fetch_prices_via_cli(non_stable_assets: list[str]) -> dict[str, float]:
    """Best-effort CLI-mode pricing: guesses each asset's USD pair as
    "<asset>USD" (e.g. "BTC" -> "BTCUSD") and fetches its ticker. This is a
    guess, not a guarantee -- a pair with no direct USD quote (USDT-quoted
    only, see config/discovery.yaml's allowed_quote_currencies) will fail
    here and show up in the resulting PortfolioValuation's pricing_errors;
    that's expected in CLI mode, not a bug to silently work around."""
    prices: dict[str, float] = {}
    for asset in non_stable_assets:
        pair = _infer_usd_pair(asset)
        try:
            payload = run_kraken_cli(["ticker", pair])
            result = payload.get("result", payload) if isinstance(payload, dict) else payload
            ticker = next(iter(result.values())) if isinstance(result, dict) and result else None
            if ticker:
                prices[asset] = float(ticker["c"][0])
        except Exception as e:
            print(f"  ! could not price {asset} via {pair}: {e}", file=sys.stderr)
    return prices


def main():
    try:
        payload = run_kraken_cli(["balance"])
    except RuntimeError as e:
        print(f"! {e}", file=sys.stderr)
        sys.exit(1)

    raw_balances = payload.get("result", payload) if isinstance(payload, dict) else payload
    balances = normalize_balances(raw_balances)

    non_stable = [a for a in balances if a not in STABLE_ASSETS]
    prices = _fetch_prices_via_cli(non_stable)

    valuation = usd_value_of_balances(balances, prices)
    print(json.dumps(valuation.to_dict(), indent=2))
    if valuation.pricing_errors:
        print("\nWarning: some holdings could not be priced (see pricing_errors above) --", file=sys.stderr)
        print("total_usd is understated, not wrong-in-the-optimistic-direction. Investigate before trusting it.", file=sys.stderr)


if __name__ == "__main__":
    main()
