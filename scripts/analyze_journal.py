#!/usr/bin/env python3
"""Analyzes journal/paper_trades.jsonl (and, once there's real live history,
journal/trades.jsonl) to surface what's actually working and what isn't --
instead of trusting a backtest or a gut feeling.

This is how "Claude learns from trading" happens in this project: NOT a
black-box auto-tuner that silently reshapes config/risk.yaml or
config/discovery.yaml on its own, but a deterministic, auditable report a
human reviews before anything changes -- the same evidence-before-action
discipline CLAUDE.md rule 7 already requires for discovery thresholds. See
.claude/skills/review-trading-performance/SKILL.md for how this report is
meant to be turned into an actual (human-approved) config change, not a
silent one.

Usage:
    python3 scripts/analyze_journal.py                              # paper journal, default
    python3 scripts/analyze_journal.py --journal journal/trades.jsonl
"""
from __future__ import annotations
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL = REPO_ROOT / "journal" / "paper_trades.jsonl"

# Matches a dollar amount, plain number, or percentage embedded in a
# not_traded reason string (e.g. "$4.10", "-15.2%", "500") -- used to
# collapse reasons that differ only in their specific number (see
# not_traded_reason_counts()) into one bucket.
_NUMERIC_RE = re.compile(r"\$?-?\d[\d,]*\.?\d*%?")

# Below this many closed trades, a per-tier/per-strategy/per-reason
# breakdown is noise, not signal -- the same "don't chase a small sample"
# caution docs/STRATEGY.md's "Judging a backtest" applies to backtests.
MIN_TRADES_FOR_BREAKDOWN = 10


def load_cycles(path: Path) -> list[dict]:
    """Fail-safe per line, not fail-crash for the whole file -- one
    malformed/partial line (e.g. a crash mid-write) must not throw away
    every other cycle's real history."""
    if not path.exists():
        return []
    cycles = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            cycles.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return cycles


def extract_closed_trades(cycles: list[dict]) -> list[dict]:
    """Pulls every sell/partial_sell action out of every cycle entry -- this
    is the actual realized-outcome data, as opposed to a cycle's
    point-in-time portfolio value (which mixes in unrealized mark-to-market
    swings on still-open positions, the exact pitfall documented in
    docs/STRATEGY.md's macd_crossover backtest caution)."""
    trades = []
    for cycle in cycles:
        for action in cycle.get("actions") or []:
            if action.get("type") in ("sell", "partial_sell"):
                trades.append(action)
    return trades


def not_traded_reason_counts(cycles: list[dict]) -> Counter:
    """Counts why eligible candidates were skipped, bucketed by the reason
    string with its parenthetical detail dropped AND any embedded number/
    dollar-amount/percentage normalized away. Most reasons embed a specific
    figure per candidate -- some after the stable text (e.g. "no open slots
    (max_concurrent_positions=15)"), some before it (e.g. "sized position
    $4.10 below min_trade_usd ($5.00)") -- so paren-stripping alone isn't
    enough; without the numeric normalization too, "$4.10" vs "$3.00" would
    each look like a unique reason and nothing would ever visibly aggregate."""
    counts = Counter()
    for cycle in cycles:
        for entry in (cycle.get("not_traded") or {}).values():
            reason = entry.get("reason", "") if isinstance(entry, dict) else str(entry)
            if not reason:
                continue
            bucket = reason.split(" (")[0].split(" -- ")[0]
            bucket = _NUMERIC_RE.sub("#", bucket).strip()[:60]
            if bucket:
                counts[bucket] += 1
    return counts


def summarize(trades: list[dict], group_key: str) -> dict[str, dict]:
    """Aggregates full-exit ("sell") trades by `group_key` ("tier",
    "strategy", "reason", or "pair") -> {n, win_rate_pct, avg_return_pct}.
    Partial profit-takes are excluded -- see the docstring note in main()
    for why they're not a closed-trade return the same way a full exit is."""
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.get("type") != "sell":
            continue
        key = t.get(group_key) or "unknown"
        if group_key == "reason":
            key = str(key).split(" (")[0]
        ret = t.get("return_pct")
        if ret is not None:
            groups[str(key)].append(ret)
    out = {}
    for key, rets in groups.items():
        wins = sum(1 for r in rets if r > 0)
        out[key] = {
            "n": len(rets),
            "win_rate_pct": (wins / len(rets)) * 100,
            "avg_return_pct": sum(rets) / len(rets),
        }
    return out


def _print_breakdown(title: str, groups: dict[str, dict]) -> None:
    print(f"\n{title}:")
    if not groups:
        print("  (no closed trades yet)")
        return
    for key, s in sorted(groups.items(), key=lambda kv: -kv[1]["avg_return_pct"]):
        flag = "" if s["n"] >= MIN_TRADES_FOR_BREAKDOWN else "  (small sample)"
        print(f"  {key:<28} n={s['n']:<4} win_rate={s['win_rate_pct']:>5.1f}%  "
              f"avg_return={s['avg_return_pct']:>+7.2f}%{flag}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    args = ap.parse_args()

    cycles = load_cycles(args.journal)
    if not cycles:
        print(f"No cycles found in {args.journal} -- nothing to analyze yet.")
        return

    trades = extract_closed_trades(cycles)
    full_exits = [t for t in trades if t.get("type") == "sell"]

    print(f"Journal: {args.journal}")
    print(f"Cycles logged: {len(cycles)}")
    print(f"Closed trades (full exits): {len(full_exits)}")
    print(f"Partial profit-takes: {len(trades) - len(full_exits)}")

    if len(full_exits) < MIN_TRADES_FOR_BREAKDOWN:
        print(f"\nFewer than {MIN_TRADES_FOR_BREAKDOWN} closed trades so far -- every breakdown below is "
              f"NOISE, not yet a pattern worth acting on. Keep accumulating cycles before drawing conclusions.")

    _print_breakdown("By exit reason", summarize(full_exits, "reason"))
    _print_breakdown("By tier", summarize(full_exits, "tier"))
    _print_breakdown("By strategy", summarize(full_exits, "strategy"))
    _print_breakdown("By pair", summarize(full_exits, "pair"))

    rejections = not_traded_reason_counts(cycles)
    if rejections:
        print("\nMost common reasons an eligible candidate was NOT traded (top 10):")
        for reason, count in rejections.most_common(10):
            print(f"  {count:>5}x  {reason}")

    all_rets = [t["return_pct"] for t in full_exits if t.get("return_pct") is not None]
    if all_rets:
        wins = sum(1 for r in all_rets if r > 0)
        print(f"\nOverall: {len(all_rets)} closed trades, {wins}/{len(all_rets)} "
              f"({wins / len(all_rets) * 100:.1f}%) winners, avg return {sum(all_rets) / len(all_rets):+.2f}%")

    print("\nThis report describes what happened -- it does not recommend a specific config change on its "
          "own. See .claude/skills/review-trading-performance/SKILL.md for how to turn a real pattern here "
          "into a proposed, human-reviewed adjustment, not an automatic one.")


if __name__ == "__main__":
    main()
