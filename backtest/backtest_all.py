#!/usr/bin/env python3
"""Backtest EVERY currently-eligible discovered token (via
research/discover_candidates.py) against EVERY strategy in
backtest/strategies.py, with walk-forward (train/test) validation, producing
one consolidated, ranked report. This is "backtest everything" -- the
comprehensive sibling to the single-token workflow in
.claude/skills/backtest-strategy.

Always includes SOL and BTC (the regime-filter reference coin) even if
neither shows up as a "discovered candidate" this run, since they're
reference assets worth knowing strategy behavior on regardless.

Live token history is fetched by contract address (see
backtest/fetch_history.py's fetch_market_chart_by_contract), so this works
even for tokens with no CoinGecko "coin id" of their own -- the same trick
that makes paper_trading/run_paper_cycle.py able to trade freshly-discovered
tokens.

Usage:
    python3 backtest/backtest_all.py
    python3 backtest/backtest_all.py --history-days 90 --max-candidates 15
    python3 backtest/backtest_all.py --skip-discovery   # SOL + BTC only, fast
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

CORE_REFERENCE_COINS = [
    {"symbol": "SOL", "coin_id": "solana"},
    {"symbol": "BTC", "coin_id": "bitcoin"},  # regime-filter reference (config/risk.yaml)
]


def disco_args(args: argparse.Namespace) -> argparse.Namespace:
    """Adapts this script's own CLI args into the shape
    research.discover_candidates.evaluate_candidate expects. Defaults mirror
    config/discovery.yaml's safety/tier block -- see that file's header for
    why this isn't parsed from the YAML directly. All overridable via this
    script's own flags, same as research/discover_candidates.py and
    paper_trading/run_paper_cycle.py -- keep the three in sync by hand."""
    return argparse.Namespace(
        request_delay=args.request_delay,
        min_liquidity_usd=args.min_liquidity_usd, min_holder_count=args.min_holder_count,
        min_organic_score=args.min_organic_score, min_pool_age_hours=args.min_pool_age_hours,
        max_top_holder_pct=args.max_top_holder_pct,
        require_mint_renounced=True, require_freeze_renounced=True, always_rugcheck=False,
        cross_check_dexscreener=False,
        blue_chip_mcap_usd=args.blue_chip_mcap_usd, blue_chip_holder_count=args.blue_chip_holder_count,
        established_mcap_usd=args.established_mcap_usd, established_holder_count=args.established_holder_count,
    )


def discover_eligible(args) -> list[dict]:
    candidates = disco.gather_candidates(argparse.Namespace(
        no_organic=False, no_trending=False, no_recent=False, limit_per_source=args.limit_per_source,
    ))
    mints = list(candidates.keys())[: args.max_candidates]
    eligible = []
    for mint in mints:
        result = disco.evaluate_candidate(mint, candidates[mint], disco_args(args))
        if result["eligible"]:
            eligible.append({"symbol": result["symbol"], "mint": mint, "tier": result["tier"]})
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
                     help="Matches the single-token workflow's default. Was 90, raised after fixing "
                          "backtest/fetch_history.py's daily-resampling bug (CoinGecko returns hourly data for "
                          "days<=90, silently making a '30-day SMA' a ~30-hour one) -- with genuinely daily bars, "
                          "90 days only leaves ~27 test-window days after a 70/30 walk-forward split, too short "
                          "for a 30-day SMA to show more than one or two real crossovers. Freshly-discovered "
                          "tokens without 180d of history still work fine (main() already skips/uses whatever "
                          "history exists if under 20 points).")
    ap.add_argument("--max-candidates", type=int, default=40)
    ap.add_argument("--limit-per-source", type=int, default=15)
    ap.add_argument("--request-delay", type=float, default=0.4)
    ap.add_argument("--skip-discovery", action="store_true", help="Only backtest SOL + BTC (fast, no discovery pass)")
    # Discovery safety/tier thresholds -- same flags and defaults as
    # research/discover_candidates.py and paper_trading/run_paper_cycle.py,
    # mirroring config/discovery.yaml. Override here if you want this run to
    # explore a looser/tighter universe than the live defaults.
    ap.add_argument("--min-liquidity-usd", type=float, default=250_000)
    ap.add_argument("--min-holder-count", type=int, default=500)
    ap.add_argument("--min-organic-score", type=float, default=40)
    ap.add_argument("--min-pool-age-hours", type=float, default=72)
    ap.add_argument("--max-top-holder-pct", type=float, default=20.0)
    ap.add_argument("--blue-chip-mcap-usd", type=float, default=50_000_000)
    ap.add_argument("--blue-chip-holder-count", type=int, default=10_000)
    ap.add_argument("--established-mcap-usd", type=float, default=5_000_000)
    ap.add_argument("--established-holder-count", type=int, default=2_000)
    args = ap.parse_args()

    assets = [{"symbol": c["symbol"], "cache_key": c["coin_id"], "kind": "coin", "ref": c["coin_id"], "tier": "reference"}
              for c in CORE_REFERENCE_COINS]

    if not args.skip_discovery:
        print("Running discovery...")
        eligible = discover_eligible(args)
        print(f"{len(eligible)} eligible candidates to backtest.\n")
        for c in eligible:
            assets.append({"symbol": c["symbol"], "cache_key": f"contract_solana_{c['mint']}",
                            "kind": "contract", "ref": c["mint"], "tier": c.get("tier")})

    all_results = []
    for a in assets:
        print(f"Fetching + backtesting {a['symbol']:<10} ({a['kind']}: {a['ref'][:20]}...)")
        try:
            if a["kind"] == "coin":
                payload = fh.fetch_market_chart(a["ref"], args.history_days)
            else:
                payload = fh.fetch_market_chart_by_contract(a["ref"], args.history_days)
            n_points = len(payload.get("prices", []))
            if n_points < 20:
                print(f"  ! only {n_points} price points -- too little history to backtest meaningfully, skipping")
                continue
            fh.save_cache(a["cache_key"], args.history_days, payload)
        except Exception as e:
            print(f"  ! could not fetch history: {e}")
            continue
        time.sleep(args.request_delay)
        rows = backtest_asset(a["cache_key"], args.history_days)
        all_results.append({"symbol": a["symbol"], "mint_or_id": a["ref"], "tier": a["tier"], "results": rows})

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"backtest_all_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(all_results, indent=2))

    # ---- summary table ----
    print(f"\n{'=' * 92}")
    print(f"{'Symbol':<10}{'Strategy':<20}{'Test Return':>13}{'Drawdown':>11}{'WinRate':>9}{'Trades':>8}{'Overfit':>9}")
    print("-" * 92)
    ensemble_returns = []
    best_per_asset = []
    for r in all_results:
        clean_rows = [row for row in r["results"] if "error" not in row]
        for row in clean_rows:
            flag = "HIGH" if row["overfit_risk_high"] else "ok"
            wr = f"{row['test_win_rate_pct']:.0f}%" if row["test_win_rate_pct"] is not None else "n/a"
            print(f"{r['symbol']:<10}{row['strategy']:<20}{row['test_return_pct']:>+12.2f}%"
                  f"{row['test_max_drawdown_pct']:>10.2f}%{wr:>9}{row['test_round_trips']:>8}{flag:>9}")
            if row["strategy"] == "adaptive_ensemble" and not row["overfit_risk_high"]:
                ensemble_returns.append(row["test_return_pct"])
        for row in r["results"]:
            if "error" in row:
                print(f"{r['symbol']:<10}{row['strategy']:<20} ERROR: {row['error']}")
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
            print(f"  {symbol:<10} ({tier or '?':<11}) {strat:<20} {ret:+.2f}%")
    print(f"\nFull report saved to {out_path}")
    print("\nA positive out-of-sample number is a candidate, not a guarantee -- re-check drawdown against "
          "config/risk.yaml's circuit_breaker_floor_usd, and re-run this periodically (docs/STRATEGY.md "
          "'Judging a backtest'). This never trades anything -- it's a research tool.")


if __name__ == "__main__":
    main()
