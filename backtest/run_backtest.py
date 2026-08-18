#!/usr/bin/env python3
"""CLI to run and compare strategies from strategies.py against cached price
history from fetch_history.py, and save a JSON report to backtest/results/.

Usage:
    python3 backtest/fetch_history.py --coin solana --days 180
    python3 backtest/run_backtest.py --coin solana --days 180 --strategy all
    python3 backtest/run_backtest.py --coin solana --days 180 --strategy adaptive_ensemble --walk-forward
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from engine import run_backtest, run_walk_forward, format_report, assess_overfit  # noqa: E402
from strategies import STRATEGIES  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"


def _summary_row(name: str, coin: str, days: int, result) -> dict:
    return {
        "strategy": name,
        "coin": coin,
        "days": days,
        "total_return_pct": result.total_return_pct,
        "num_round_trips": result.num_round_trips,
        "win_rate_pct": result.win_rate_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe_approx": result.sharpe_approx,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True, help="CoinGecko coin id (must match a cached file)")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--strategy", default="all", choices=list(STRATEGIES) + ["all"])
    ap.add_argument("--fee-bps", type=float, default=30)
    ap.add_argument("--slippage-bps", type=float, default=50)
    ap.add_argument("--walk-forward", action="store_true",
                     help="Chronological 70/30 train/test split to check for overfitting, instead of one in-sample run.")
    ap.add_argument("--split", type=float, default=0.7)
    args = ap.parse_args()

    names = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    summary = []

    for name in names:
        fn = STRATEGIES[name]
        print("=" * 44)
        print(f"Strategy: {name}")

        if args.walk_forward:
            train, test = run_walk_forward(args.coin, args.days, fn, split=args.split,
                                            fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
            print("-- TRAIN (in-sample) --")
            print(format_report(train))
            print("-- TEST (out-of-sample) --")
            print(format_report(test))
            gap, overfit_flag = assess_overfit(train, test)
            if overfit_flag:
                print(f"⚠ Overfit risk: HIGH (train {train.total_return_pct:+.1f}% vs test {test.total_return_pct:+.1f}%, "
                      f"gap {gap:+.1f}pp) -- do not trust this result for live trading without investigation.")
            else:
                print(f"Overfit risk: low/moderate (gap {gap:+.1f}pp)")
            row = _summary_row(name, args.coin, args.days, test)
            row["train_total_return_pct"] = train.total_return_pct
            row["overfit_risk_high"] = overfit_flag
            summary.append(row)
        else:
            result = run_backtest(args.coin, args.days, fn, fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
            print(format_report(result))
            summary.append(_summary_row(name, args.coin, args.days, result))

    print("=" * 44)
    mode = "wf" if args.walk_forward else "is"
    out_path = RESULTS_DIR / f"{args.coin}_{args.days}d_{mode}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"Saved comparison report to {out_path}")
    best = max(summary, key=lambda s: s["total_return_pct"])
    label = "out-of-sample" if args.walk_forward else "raw"
    print(f"Best by {label} return: {best['strategy']} ({best['total_return_pct']:+.2f}%) "
          f"-- do not pick a strategy on return alone, check drawdown, win rate, and overfit risk too.")


if __name__ == "__main__":
    main()
