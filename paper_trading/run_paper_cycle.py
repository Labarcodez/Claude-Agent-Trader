#!/usr/bin/env python3
"""Paper-trading simulator: runs the SAME discovery + strategy + risk logic
the live `trade-cycle` skill uses, against real live market data, but never
touches a real wallet. Fills are simulated at current market price with the
same fee/slippage assumptions as backtest/engine.py. State lives in
state/paper_portfolio.json; every cycle appends to journal/paper_trades.jsonl
-- kept completely separate from journal/trades.jsonl (real trading history).

Why this exists: it validates the full autonomous pipeline end-to-end --
including on tokens research/discover_candidates.py finds fresh, which have
no track record of their own -- before any real money is at risk. It's
runnable from a session with no Phantom MCP connection at all, since it only
reads public market data (Jupiter, CoinGecko, RugCheck). Run it repeatedly
(e.g. via /loop) to build a track record while deciding whether to trust the
system live; see docs/RUNBOOK.md "Paper trading before going live".

Simplifications vs. the live trade-cycle skill (documented, not hidden):
  - No real swap quote, so no live slippage/price-impact check -- a fixed
    fee_bps + slippage_bps cost is assumed instead (same convention as
    backtest/engine.py).
  - No max_daily_trade_count / max_daily_volume_usd / min_hours_between_trades
    cadence caps -- this script is typically run manually or via /loop at a
    deliberate interval, so cadence is controlled by how often you run it.
  - Core position sizing, tiering, volatility scaling, portfolio heat,
    regime filter, and stop-loss/take-profit/trailing-stop ARE all real,
    reusing the exact same code (backtest/strategies.py, research/discover_candidates.py)
    the live skill is documented to use.

Usage:
    python3 paper_trading/run_paper_cycle.py
    python3 paper_trading/run_paper_cycle.py --reset       # wipe paper state, restart at starting capital
    python3 paper_trading/run_paper_cycle.py --max-candidates 10
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
from backtest import strategies as strat  # noqa: E402

STATE_PATH = REPO_ROOT / "state" / "paper_portfolio.json"
JOURNAL_PATH = REPO_ROOT / "journal" / "paper_trades.jsonl"

TIER_MULTIPLIERS = {"blue_chip": 1.0, "established": 0.7, "emerging": 0.4}


# ---- State ------------------------------------------------------------------

def load_state(starting_capital_usd: float, reset: bool) -> dict:
    if STATE_PATH.exists() and not reset:
        return json.loads(STATE_PATH.read_text())
    state = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "starting_capital_usd": starting_capital_usd,
        "cash_usd": starting_capital_usd,
        "positions": {},   # mint -> {symbol, quantity, entry_price_usd, entry_time, tier, peak_price_usd}
        "closed_trades": [],
        "peak_portfolio_value_usd": starting_capital_usd,
        "circuit_breaker": {"tripped": False, "reason": None, "tripped_at": None},
        "cycles_run": 0,
    }
    save_state(state)
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def append_journal(entry: dict) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---- Pricing ------------------------------------------------------------------

def current_price(mint: str) -> float | None:
    """Live USD price for any mint, via Jupiter's search endpoint -- works for
    core assets, discovered candidates, and existing positions uniformly."""
    results = disco._get_json(f"{disco.JUPITER_BASE}/search?query={mint}")
    for r in results or []:
        if r.get("id") == mint:
            return r.get("usdPrice")
    return None


def portfolio_value_usd(state: dict, prices: dict[str, float]) -> float:
    total = state["cash_usd"]
    for mint, pos in state["positions"].items():
        price = prices.get(mint)
        if price is not None:
            total += pos["quantity"] * price
    return total


# ---- Regime filter ------------------------------------------------------------

def regime_allows_new_entries(reference_coin: str, sma_window_days: int) -> bool:
    try:
        payload = fh.fetch_market_chart(reference_coin, days=sma_window_days + 5)
    except Exception as e:
        print(f"  ! regime filter data unavailable ({e}) -- defaulting to conservative (no new entries)", file=sys.stderr)
        return False
    closes = [p for _, p in payload.get("prices", [])]
    if len(closes) < sma_window_days:
        return False
    sma = sum(closes[-sma_window_days:]) / sma_window_days
    return closes[-1] > sma


# ---- Signal generation --------------------------------------------------------

def get_price_history_closes(mint: str, days: int) -> list[float] | None:
    try:
        payload = fh.fetch_market_chart_by_contract(mint, days=days)
    except Exception:
        return None
    prices = payload.get("prices") or []
    if len(prices) < 15:  # not enough for even the fastest indicator to warm up meaningfully
        return None
    return [p for _, p in prices]


def compute_signal(closes: list[float]) -> str:
    return strat.adaptive_ensemble(closes, len(closes) - 1, {})


def realized_vol_pct(closes: list[float]) -> float | None:
    vol = strat.realized_vol(closes, window=min(20, max(2, len(closes) - 1)))
    return vol * 100 if vol is not None else None


# ---- Sizing & risk --------------------------------------------------------------

def size_position(tier: str, portfolio_value: float, closes: list[float] | None, args) -> float:
    base = min(args.max_position_usd, portfolio_value * args.max_position_fraction)
    base *= TIER_MULTIPLIERS.get(tier, 0.4)
    if closes is None:
        # no history -- trade_cycle SKILL.md's rule: minimum size only, no vol scaling
        return max(args.min_trade_usd, min(base, args.min_trade_usd * 2))
    vol_pct = realized_vol_pct(closes)
    if vol_pct is None or vol_pct == 0:
        mult = 1.0
    else:
        mult = args.target_daily_volatility_pct / vol_pct
        mult = max(args.volatility_size_min_mult, min(args.volatility_size_max_mult, mult))
    return base * mult


def portfolio_heat_pct(state: dict, prices: dict[str, float], stop_loss_pct: float) -> float:
    value = portfolio_value_usd(state, prices)
    if value <= 0:
        return 0.0
    heat_usd = 0.0
    for mint, pos in state["positions"].items():
        price = prices.get(mint)
        if price is not None:
            heat_usd += pos["quantity"] * price * stop_loss_pct
    return heat_usd / value


# ---- Main cycle -----------------------------------------------------------------

def run_cycle(args):
    cycle_start = datetime.now(timezone.utc)
    state = load_state(args.starting_capital_usd, args.reset)

    if state["circuit_breaker"]["tripped"]:
        print(f"Paper circuit breaker is tripped: {state['circuit_breaker']['reason']}")
        print("Run with --reset to start a fresh paper portfolio, or investigate before continuing.")
        return

    print(f"=== Paper trade cycle -- {cycle_start.isoformat()} ===")
    print(f"Cash: ${state['cash_usd']:.2f}  |  Open positions: {len(state['positions'])}  |  Cycles run so far: {state['cycles_run']}")

    # ---- discovery ----
    print("\nRunning discovery...")
    candidates = disco.gather_candidates(argparse.Namespace(
        no_organic=False, no_trending=False, no_recent=False, limit_per_source=args.limit_per_source,
    ))
    mints = list(candidates.keys())[: args.max_candidates]
    eligible: dict[str, dict] = {}
    rejected_count = 0
    for mint in mints:
        result = disco.evaluate_candidate(mint, candidates[mint], disco_args(args))
        if result["eligible"]:
            eligible[mint] = result
        else:
            rejected_count += 1
    print(f"Discovery: {len(candidates)} found, {len(mints)} evaluated, {len(eligible)} eligible, {rejected_count} rejected.")

    # include existing paper positions even if they fell out of discovery this cycle
    tracked_mints = set(eligible) | set(state["positions"])
    for mint in disco.CORE_ASSET_MINTS:
        tracked_mints.discard(mint)

    prices: dict[str, float] = {}
    for mint in tracked_mints | disco.CORE_ASSET_MINTS:
        time.sleep(args.request_delay)
        price = current_price(mint)
        if price is not None:
            prices[mint] = price

    port_value = portfolio_value_usd(state, prices)
    if port_value > state["peak_portfolio_value_usd"]:
        state["peak_portfolio_value_usd"] = port_value

    # ---- circuit breaker check ----
    dd_from_peak = (port_value - state["peak_portfolio_value_usd"]) / state["peak_portfolio_value_usd"] if state["peak_portfolio_value_usd"] else 0
    if port_value <= args.circuit_breaker_floor_usd or dd_from_peak <= -args.circuit_breaker_daily_loss_pct:
        reason = f"portfolio ${port_value:.2f} hit floor/drawdown limit (peak was ${state['peak_portfolio_value_usd']:.2f})"
        state["circuit_breaker"] = {"tripped": True, "reason": reason, "tripped_at": cycle_start.isoformat()}
        save_state(state)
        append_journal({"timestamp": cycle_start.isoformat(), "type": "circuit_breaker_trip", "reason": reason, "portfolio_value_usd": port_value})
        print(f"\n!!! PAPER CIRCUIT BREAKER TRIPPED: {reason}")
        return

    # ---- regime filter ----
    allow_new_entries = regime_allows_new_entries(args.regime_reference_coin, args.regime_sma_window_days)
    print(f"\nRegime filter (vs {args.regime_reference_coin} {args.regime_sma_window_days}d SMA): "
          f"{'risk-ON (new entries allowed)' if allow_new_entries else 'risk-OFF (new entries blocked)'}")

    actions = []

    # ---- manage existing positions first (exits are never regime-gated) ----
    for mint, pos in list(state["positions"].items()):
        price = prices.get(mint)
        if price is None:
            continue
        pos["peak_price_usd"] = max(pos.get("peak_price_usd", pos["entry_price_usd"]), price)
        ret = (price / pos["entry_price_usd"]) - 1
        drawdown_from_peak = (price / pos["peak_price_usd"]) - 1
        exit_reason = None
        if ret <= -args.stop_loss_pct:
            exit_reason = f"stop-loss ({ret:+.1%})"
        elif ret >= args.take_profit_pct:
            exit_reason = f"take-profit ({ret:+.1%})"
        elif ret > 0 and drawdown_from_peak <= -args.trailing_stop_pct:
            exit_reason = f"trailing-stop ({drawdown_from_peak:+.1%} from peak)"
        if exit_reason is None and mint in eligible:
            closes = get_price_history_closes(mint, args.history_days)
            if closes and compute_signal(closes) == "sell":
                exit_reason = "strategy sell signal"
        if exit_reason:
            proceeds = pos["quantity"] * price * (1 - args.fee_bps / 10_000 - args.slippage_bps / 10_000)
            state["cash_usd"] += proceeds
            state["closed_trades"].append({
                "symbol": pos["symbol"], "mint": mint, "entry_price_usd": pos["entry_price_usd"],
                "exit_price_usd": price, "return_pct": ret * 100, "reason": exit_reason,
                "closed_at": cycle_start.isoformat(),
            })
            actions.append({"type": "sell", "symbol": pos["symbol"], "mint": mint, "reason": exit_reason, "return_pct": ret * 100})
            del state["positions"][mint]

    # ---- consider new entries ----
    if allow_new_entries:
        open_slots = args.max_concurrent_positions - len(state["positions"])
        emerging_open = sum(1 for p in state["positions"].values() if p.get("tier") == "emerging")
        for mint, result in eligible.items():
            if open_slots <= 0:
                break
            if mint in state["positions"]:
                continue
            price = prices.get(mint)
            if price is None:
                continue
            tier = result["tier"]
            if tier == "emerging" and emerging_open >= args.max_emerging_tier_positions:
                continue
            closes = get_price_history_closes(mint, args.history_days)
            time.sleep(args.request_delay)
            signal = compute_signal(closes) if closes else None
            if signal != "buy" and closes is not None:
                continue  # only trade no-history tokens opportunistically-small; require an actual buy signal when history exists
            size_usd = size_position(tier, port_value, closes, args)
            if size_usd < args.min_trade_usd or size_usd > state["cash_usd"]:
                continue
            projected_heat = portfolio_heat_pct(state, prices, args.stop_loss_pct) + (size_usd * args.stop_loss_pct / port_value if port_value else 0)
            if projected_heat > args.max_portfolio_heat_pct:
                continue
            fill_price = price * (1 + args.slippage_bps / 10_000)
            quantity = (size_usd * (1 - args.fee_bps / 10_000)) / fill_price
            state["cash_usd"] -= size_usd
            state["positions"][mint] = {
                "symbol": result["symbol"], "quantity": quantity, "entry_price_usd": fill_price,
                "entry_time": cycle_start.isoformat(), "tier": tier, "peak_price_usd": fill_price,
            }
            actions.append({"type": "buy", "symbol": result["symbol"], "mint": mint, "tier": tier,
                             "size_usd": size_usd, "fill_price": fill_price,
                             "signal_basis": "buy signal" if closes else "no-history minimum-size entry"})
            open_slots -= 1
            if tier == "emerging":
                emerging_open += 1

    final_prices = dict(prices)
    for mint in state["positions"]:
        if mint not in final_prices:
            final_prices[mint] = current_price(mint) or prices.get(mint, 0)
    final_value = portfolio_value_usd(state, final_prices)
    state["cycles_run"] += 1
    save_state(state)

    append_journal({
        "timestamp": cycle_start.isoformat(),
        "type": "cycle",
        "portfolio_value_usd_before": port_value,
        "portfolio_value_usd_after": final_value,
        "regime_allows_new_entries": allow_new_entries,
        "discovery": {"found": len(candidates), "evaluated": len(mints), "eligible": len(eligible), "rejected": rejected_count},
        "actions": actions,
        "open_positions": len(state["positions"]),
    })

    print(f"\nActions this cycle: {len(actions)}")
    for a in actions:
        if a["type"] == "buy":
            print(f"  BUY  {a['symbol']:>10}  ${a['size_usd']:.2f}  tier={a['tier']:<10}  ({a['signal_basis']})")
        else:
            print(f"  SELL {a['symbol']:>10}  {a['reason']}  return={a['return_pct']:+.1f}%")
    print(f"\nPortfolio value: ${port_value:.2f} -> ${final_value:.2f}  "
          f"(total return since start: {(final_value / state['starting_capital_usd'] - 1) * 100:+.1f}%)")
    print(f"Open positions: {len(state['positions'])}  |  Closed trades all-time: {len(state['closed_trades'])}")
    print(f"State saved to {STATE_PATH}")
    print(f"Journal appended to {JOURNAL_PATH}")


def disco_args(args) -> argparse.Namespace:
    """Adapts this script's CLI args into the shape research.discover_candidates.evaluate_candidate expects."""
    return argparse.Namespace(
        request_delay=args.request_delay,
        min_liquidity_usd=args.min_liquidity_usd,
        min_holder_count=args.min_holder_count,
        min_organic_score=args.min_organic_score,
        min_pool_age_hours=args.min_pool_age_hours,
        max_top_holder_pct=args.max_top_holder_pct,
        require_mint_renounced=True,
        require_freeze_renounced=True,
        always_rugcheck=False,
        cross_check_dexscreener=False,  # keep paper cycles fast/light on free APIs by default
        blue_chip_mcap_usd=args.blue_chip_mcap_usd,
        blue_chip_holder_count=args.blue_chip_holder_count,
        established_mcap_usd=args.established_mcap_usd,
        established_holder_count=args.established_holder_count,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="Wipe paper state and restart at --starting-capital-usd")
    ap.add_argument("--starting-capital-usd", type=float, default=50.0)
    ap.add_argument("--max-candidates", type=int, default=15)
    ap.add_argument("--limit-per-source", type=int, default=8)
    ap.add_argument("--request-delay", type=float, default=0.4)
    ap.add_argument("--history-days", type=int, default=30)
    # mirrors config/risk.yaml -- keep in sync by hand, same convention as research/discover_candidates.py
    ap.add_argument("--max-position-fraction", dest="max_position_fraction", type=float, default=0.30)
    ap.add_argument("--max-position-usd", dest="max_position_usd", type=float, default=20.0)
    ap.add_argument("--min-trade-usd", dest="min_trade_usd", type=float, default=5.0)
    ap.add_argument("--max-concurrent-positions", dest="max_concurrent_positions", type=int, default=3)
    ap.add_argument("--max-emerging-tier-positions", dest="max_emerging_tier_positions", type=int, default=2)
    ap.add_argument("--target-daily-volatility-pct", dest="target_daily_volatility_pct", type=float, default=3.0)
    ap.add_argument("--volatility-size-min-mult", dest="volatility_size_min_mult", type=float, default=0.5)
    ap.add_argument("--volatility-size-max-mult", dest="volatility_size_max_mult", type=float, default=1.5)
    ap.add_argument("--max-portfolio-heat-pct", dest="max_portfolio_heat_pct", type=float, default=0.12)
    ap.add_argument("--stop-loss-pct", dest="stop_loss_pct", type=float, default=0.15)
    ap.add_argument("--take-profit-pct", dest="take_profit_pct", type=float, default=0.35)
    ap.add_argument("--trailing-stop-pct", dest="trailing_stop_pct", type=float, default=0.12)
    ap.add_argument("--circuit-breaker-floor-usd", dest="circuit_breaker_floor_usd", type=float, default=20.0)
    ap.add_argument("--circuit-breaker-daily-loss-pct", dest="circuit_breaker_daily_loss_pct", type=float, default=0.25)
    ap.add_argument("--regime-reference-coin", dest="regime_reference_coin", default="bitcoin")
    ap.add_argument("--regime-sma-window-days", dest="regime_sma_window_days", type=int, default=30)
    ap.add_argument("--fee-bps", dest="fee_bps", type=float, default=30.0)
    ap.add_argument("--slippage-bps", dest="slippage_bps", type=float, default=50.0)
    # discovery safety thresholds -- mirrors config/discovery.yaml
    ap.add_argument("--min-liquidity-usd", dest="min_liquidity_usd", type=float, default=250_000)
    ap.add_argument("--min-holder-count", dest="min_holder_count", type=int, default=500)
    ap.add_argument("--min-organic-score", dest="min_organic_score", type=float, default=40)
    ap.add_argument("--min-pool-age-hours", dest="min_pool_age_hours", type=float, default=72)
    ap.add_argument("--max-top-holder-pct", dest="max_top_holder_pct", type=float, default=20.0)
    ap.add_argument("--blue-chip-mcap-usd", dest="blue_chip_mcap_usd", type=float, default=50_000_000)
    ap.add_argument("--blue-chip-holder-count", dest="blue_chip_holder_count", type=int, default=10_000)
    ap.add_argument("--established-mcap-usd", dest="established_mcap_usd", type=float, default=5_000_000)
    ap.add_argument("--established-holder-count", dest="established_holder_count", type=int, default=2_000)
    args = ap.parse_args()
    run_cycle(args)


if __name__ == "__main__":
    main()
