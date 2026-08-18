#!/usr/bin/env python3
"""CLI to run and compare strategies from strategies.py against cached price
history from fetch_history.py, and save a JSON report to backtest/results/.

Usage:
    python3 backtest/fetch_history.py --coin solana --days 180
    python3 backtest/run_backtest.py --coin solana --days 180 --strategy all
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from engine import run_backtest, format_report  # noqa: E402
from strategies import STRATEGIES  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True, help="CoinGecko coin id (must match a cached file)")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--strategy", default="all", choices=list(STRATEGIES) + ["all"])
    ap.add_argument("--fee-bps", type=float, default=30)
    ap.add_argument("--slippage-bps", type=float, default=50)
    args = ap.parse_args()

    names = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary = []

    for name in names:
        fn = STRATEGIES[name]
        result = run_backtest(args.coin, args.days, fn, fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
        print("=" * 44)
        print(format_report(result))
        summary.append({
            "strategy": name,
            "coin": args.coin,
            "days": args.days,
            "total_return_pct": result.total_return_pct,
            "num_round_trips": result.num_round_trips,
            "win_rate_pct": result.win_rate_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "sharpe_approx": result.sharpe_approx,
        })

    print("=" * 44)
    out_path = RESULTS_DIR / f"{args.coin}_{args.days}d_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"Saved comparison report to {out_path}")
    best = max(summary, key=lambda s: s["total_return_pct"])
    print(f"Best by raw return: {best['strategy']} ({best['total_return_pct']:+.2f}%) "
          f"-- do not pick a strategy on raw return alone, check drawdown and win rate too.")


if __name__ == "__main__":
    main()
