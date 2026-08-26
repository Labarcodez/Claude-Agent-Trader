#!/usr/bin/env python3
"""Backtest EVERY currently-eligible Kraken pair (via
research/discover_candidates.py) against EVERY strategy in
backtest/strategies.py, with walk-forward (train/test) validation, producing
one consolidated, ranked report. This is "backtest everything" -- the
comprehensive sibling to the single-pair workflow in
.claude/skills/backtest-strategy.

Always includes BTC and ETH (XBTUSD, ETHUSD) even if neither shows up in
this run's discovery output, since BTC is the regime-filter reference pair
(config/risk.yaml's regime_reference_pair) and both are worth knowing
strategy behavior on regardless.

History is fetched from Kraken's own public OHLC endpoint (see
backtest/fetch_history.py's fetch_market_chart_by_pair), so this works for
any Kraken-listed pair without needing a separate CoinGecko listing.

Usage:
    python3 backtest/backtest_all.py
    python3 backtest/backtest_all.py --history-days 180 --max-candidates 20
    python3 backtest/backtest_all.py --skip-discovery   # BTC + ETH only, fast
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from research import discover_candidates as disco  # noqa: E402
from backtest import fetch_history as fh  # noqa: E402
from backtest.engine import run_walk_forward, assess_overfit  # noqa: E402
from backtest.strategies import STRATEGIES  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"

CORE_REFERENCE_PAIRS = [
    {"altname": "XBTUSD"},   # regime-filter reference (config/risk.yaml)
    {"altname": "ETHUSD"},
]


def disco_args(args: argparse.Namespace) -> argparse.Namespace:
    """Adapts this script's own CLI args into the shape
    research.discover_candidates.evaluate_candidate expects. Defaults mirror
    config/discovery.yaml's safety/tier block -- see that file's header for
    why this isn't parsed from the YAML directly. All overridable via this
    script's own flags, same as research/discover_candidates.py and
    paper_trading/run_paper_cycle.py -- keep the three in sync by hand."""
    return argparse.Namespace(
        min_24h_quote_volume_usd=args.min_24h_quote_volume_usd,
        max_spread_bps=args.max_spread_bps,
        min_price_usd=0.000001,
        blue_chip_min_volume_usd=args.blue_chip_min_volume_usd,
        blue_chip_max_spread_bps=args.blue_chip_max_spread_bps,
        established_min_volume_usd=args.established_min_volume_usd,
        established_max_spread_bps=args.established_max_spread_bps,
    )


def discover_eligible(args) -> list[dict]:
    candidates = disco.gather_candidates(argparse.Namespace(max_candidates=args.max_candidates))
    eligible = []
    for altname, candidate in candidates.items():
        result = disco.evaluate_candidate(altname, candidate, disco_args(args))
        if result["eligible"]:
            eligible.append({"altname": altname, "tier": result["tier"]})
    return eligible


def backtest_asset(cache_key: str, days: int) -> list[dict]:
    rows = []
    for name, fn in STRATEGIES.items():
        try:
            train, test = run_walk_forward(cache_key, days, fn)
        except Exception as e:
            rows.append({"strategy": name, "error": str(e)})
            continue
        gap, overfit = assess_overfit(train, test)
        rows.append({
            "strategy": name,
            "train_return_pct": train.total_return_pct,
            "test_return_pct": test.total_return_pct,
            "test_max_drawdown_pct": test.max_drawdown_pct,
            "test_win_rate_pct": test.win_rate_pct,
            "test_round_trips": test.num_round_trips,
            "test_sharpe_approx": test.sharpe_approx,
            "overfit_gap_pct": gap,
            "overfit_risk_high": overfit,
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history-days", type=int, default=180,
                     help="Kraken's public OHLC endpoint caps daily candles at 720 (~2 years); this is silently "
                          "clamped in backtest/fetch_history.py. 180 leaves a healthy test-window after a 70/30 "
                          "walk-forward split for a 30-day SMA to show several real crossovers.")
    ap.add_argument("--max-candidates", type=int, default=30)
    ap.add_argument("--skip-discovery", action="store_true", help="Only backtest BTC + ETH (fast, no discovery pass)")
    # Discovery safety/tier thresholds -- same flags and defaults as
    # research/discover_candidates.py, mirroring config/discovery.yaml.
    # Override here if you want this run to explore a looser/tighter
    # universe than the live defaults.
    ap.add_argument("--min-24h-quote-volume-usd", dest="min_24h_quote_volume_usd", type=float, default=500_000)
    ap.add_argument("--max-spread-bps", type=float, default=50)
    ap.add_argument("--blue-chip-min-volume-usd", dest="blue_chip_min_volume_usd", type=float, default=100_000_000)
    ap.add_argument("--blue-chip-max-spread-bps", dest="blue_chip_max_spread_bps", type=float, default=10)
    ap.add_argument("--established-min-volume-usd", dest="established_min_volume_usd", type=float, default=10_000_000)
    ap.add_argument("--established-max-spread-bps", dest="established_max_spread_bps", type=float, default=25)
    args = ap.parse_args()

    assets = [{"altname": c["altname"], "tier": "reference"} for c in CORE_REFERENCE_PAIRS]

    if not args.skip_discovery:
        print("Running discovery...")
        eligible = discover_eligible(args)
        print(f"{len(eligible)} eligible candidates to backtest.\n")
        for c in eligible:
            if c["altname"] not in {a["altname"] for a in assets}:
                assets.append({"altname": c["altname"], "tier": c.get("tier")})

    all_results = []
    for a in assets:
        print(f"Fetching + backtesting {a['altname']}")
        try:
            payload = fh.fetch_market_chart_by_pair(a["altname"], args.history_days)
            n_points = len(payload.get("prices", []))
            if n_points < 20:
                print(f"  ! only {n_points} price points -- too little history to backtest meaningfully, skipping")
                continue
            fh.save_cache(f"kraken_{a['altname']}", args.history_days, payload)
        except Exception as e:
            print(f"  ! could not fetch history: {e}")
            continue
        rows = backtest_asset(f"kraken_{a['altname']}", args.history_days)
        all_results.append({"symbol": a["altname"], "tier": a["tier"], "results": rows})

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"backtest_all_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(all_results, indent=2))

    # ---- summary table ----
    print(f"\n{'=' * 92}")
    print(f"{'Symbol':<12}{'Strategy':<20}{'Test Return':>13}{'Drawdown':>11}{'WinRate':>9}{'Trades':>8}{'Overfit':>9}")
    print("-" * 92)
    ensemble_returns = []
    best_per_asset = []
    for r in all_results:
        clean_rows = [row for row in r["results"] if "error" not in row]
        for row in clean_rows:
            flag = "HIGH" if row["overfit_risk_high"] else "ok"
            wr = f"{row['test_win_rate_pct']:.0f}%" if row["test_win_rate_pct"] is not None else "n/a"
            print(f"{r['symbol']:<12}{row['strategy']:<20}{row['test_return_pct']:>+12.2f}%"
                  f"{row['test_max_drawdown_pct']:>10.2f}%{wr:>9}{row['test_round_trips']:>8}{flag:>9}")
            if row["strategy"] == "adaptive_ensemble" and not row["overfit_risk_high"]:
                ensemble_returns.append(row["test_return_pct"])
        for row in r["results"]:
            if "error" in row:
                print(f"{r['symbol']:<12}{row['strategy']:<20} ERROR: {row['error']}")
        non_overfit = [row for row in clean_rows if not row["overfit_risk_high"]]
        if non_overfit:
            best = max(non_overfit, key=lambda row: row["test_return_pct"])
            best_per_asset.append((r["symbol"], r["tier"], best["strategy"], best["test_return_pct"]))

    print("-" * 92)
    if ensemble_returns:
        avg = sum(ensemble_returns) / len(ensemble_returns)
        wins = sum(1 for x in ensemble_returns if x > 0)
        print(f"adaptive_ensemble (the default live strategy), non-overfit assets only: "
              f"n={len(ensemble_returns)}, avg out-of-sample return {avg:+.2f}%, positive on {wins}/{len(ensemble_returns)}")
    if best_per_asset:
        print("\nBest non-overfit out-of-sample strategy per asset:")
        for symbol, tier, strat, ret in sorted(best_per_asset, key=lambda x: -x[3]):
            print(f"  {symbol:<12} ({tier or '?':<11}) {strat:<20} {ret:+.2f}%")
    print(f"\nFull report saved to {out_path}")
    print("\nA positive out-of-sample number is a candidate, not a guarantee -- re-check drawdown against "
          "config/risk.yaml's circuit_breaker_floor_usd, and re-run this periodically (docs/STRATEGY.md "
          "'Judging a backtest'). This never trades anything -- it's a research tool. Fees/slippage assumed "
          "here (backtest/engine.py's fee_bps/slippage_bps defaults) model Kraken's own fee schedule, not a "
          "generic estimate -- see docs/STRATEGY.md 'Fee-aware execution'.")


if __name__ == "__main__":
    main()
