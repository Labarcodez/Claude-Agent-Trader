#!/usr/bin/env python3
"""Paper-trading simulator: runs the SAME discovery + strategy + risk logic
the live `trade-cycle` skill uses, against real live Kraken market data, but
never touches a real account. Fills are simulated at current market price
with the same fee/slippage assumptions as backtest/engine.py. State lives in
state/paper_portfolio.json; every cycle appends to journal/paper_trades.jsonl
-- kept completely separate from journal/trades.jsonl (real trading history).

Why this exists: it validates the full autonomous pipeline end-to-end --
including on pairs research/discover_candidates.py finds fresh, which have
no track record built up in this repo yet -- before any real money is at
risk. It only reads Kraken's public market-data endpoints (no API key), so
it's runnable from any session, including one where a live Kraken MCP
connection isn't set up at all. Run it repeatedly (e.g. via /loop) to build
a track record while deciding whether to trust the system live; see
docs/RUNBOOK.md "Paper trading before going live".

Note: Kraken CLI (kraken-cli) also ships its OWN built-in paper trading
engine (`kraken workspace create ... --mode paper`, `kraken paper buy/sell`)
that simulates real order mechanics (partial fills, order types, leverage on
the futures side). That's a complementary, lower-level validation layer for
order execution itself; this script is the higher-level pipeline validator
for the discovery -> regime -> strategy -> sizing -> risk chain the
adaptive_ensemble strategy actually runs on. Using both before going live is
reasonable, not redundant.

Simplifications vs. the live trade-cycle skill (documented, not hidden):
  - No real order-book depth check, so no live slippage/spread check at
    order time -- a fixed fee_bps + slippage_bps cost is assumed instead
    (same convention as backtest/engine.py).
  - No live fee-tier lookup -- the live skill queries the account's real,
    current Kraken fee tier each cycle (account/fees.py's parse_fee_tier(),
    via the MCP `volume` tool); this script always uses the flat
    --fee-bps/--slippage-bps CLI defaults instead, since there's no real
    account/fee history to look up in a simulation.
  - No account/fees.py min_edge_to_cost_multiple check -- a paper entry only
    needs a "buy" signal, not a fee-clears-costs check, so its size/entry
    logic is simpler than what trade-cycle's step 7 actually requires live.
  - No max_daily_trade_count / max_daily_volume_usd / min_hours_between_trades
    cadence caps -- this script is typically run manually or via /loop at a
    deliberate interval, so cadence is controlled by how often you run it.
  - No Kraken Earn / idle-cash yield simulation -- cash sits at 0% here,
    unlike the live skill's optional Earn allocation (config/risk.yaml).
  - Core position sizing, tiering, volatility scaling, portfolio heat,
    regime filter, and stop-loss/take-profit/trailing-stop ARE all real,
    reusing the exact same code (backtest/strategies.py,
    research/discover_candidates.py) the live skill is documented to use.

Usage:
    python3 paper_trading/run_paper_cycle.py
    python3 paper_trading/run_paper_cycle.py --reset       # wipe paper state, restart at starting capital
    python3 paper_trading/run_paper_cycle.py --max-candidates 20
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
        "positions": {},   # altname -> {symbol, quantity, entry_price_usd, entry_time, tier, peak_price_usd}
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

def current_price(altname: str) -> float | None:
    """Live USD(-ish) price for a Kraken pair altname, via the public Ticker
    endpoint -- works uniformly for core reference pairs, discovered
    candidates, and existing positions."""
    result = disco._get_json(f"{disco.KRAKEN_BASE}/Ticker?pair={altname}")
    if not result:
        return None
    ticker = next(iter(result.values()), None)
    try:
        return float(ticker["c"][0]) if ticker else None
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def portfolio_value_usd(state: dict, prices: dict[str, float]) -> float:
    total = state["cash_usd"]
    for altname, pos in state["positions"].items():
        price = prices.get(altname)
        if price is not None:
            total += pos["quantity"] * price
    return total


# ---- Regime filter ------------------------------------------------------------

def regime_allows_new_entries(reference_pair: str, sma_window_days: int) -> bool:
    try:
        payload = fh.fetch_market_chart_by_pair(reference_pair, days=sma_window_days + 5)
    except Exception as e:
        print(f"  ! regime filter data unavailable ({e}) -- defaulting to conservative (no new entries)", file=sys.stderr)
        return False
    closes = [p for _, p in payload.get("prices", [])]
    if len(closes) < sma_window_days:
        return False
    sma = sum(closes[-sma_window_days:]) / sma_window_days
    return closes[-1] > sma


# ---- Signal generation --------------------------------------------------------

def get_price_history_closes(altname: str, days: int) -> list[float] | None:
    try:
        payload = fh.fetch_market_chart_by_pair(altname, days=days)
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
    for altname, pos in state["positions"].items():
        price = prices.get(altname)
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
    candidates = disco.gather_candidates(argparse.Namespace(max_candidates=args.max_candidates))
    eligible: dict[str, dict] = {}
    rejected_count = 0
    for altname in candidates:
        result = disco.evaluate_candidate(altname, candidates[altname], disco_args(args))
        if result["eligible"]:
            eligible[altname] = result
        else:
            rejected_count += 1
    print(f"Discovery: {len(candidates)} evaluated, {len(eligible)} eligible, {rejected_count} rejected.")

    # include existing paper positions even if they fell out of discovery this cycle
    tracked = set(eligible) | set(state["positions"])

    prices: dict[str, float] = {}
    for altname in tracked:
        time.sleep(args.request_delay)
        price = current_price(altname)
        if price is not None:
            prices[altname] = price

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
    allow_new_entries = regime_allows_new_entries(args.regime_reference_pair, args.regime_sma_window_days)
    print(f"\nRegime filter (vs {args.regime_reference_pair} {args.regime_sma_window_days}d SMA): "
          f"{'risk-ON (new entries allowed)' if allow_new_entries else 'risk-OFF (new entries blocked)'}")

    actions = []

    # ---- manage existing positions first (exits are never regime-gated) ----
    for altname, pos in list(state["positions"].items()):
        price = prices.get(altname)
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
        if exit_reason is None and altname in eligible:
            closes = get_price_history_closes(altname, args.history_days)
            if closes and compute_signal(closes) == "sell":
                exit_reason = "strategy sell signal"
        if exit_reason:
            proceeds = pos["quantity"] * price * (1 - args.fee_bps / 10_000 - args.slippage_bps / 10_000)
            state["cash_usd"] += proceeds
            state["closed_trades"].append({
                "symbol": pos["symbol"], "altname": altname, "entry_price_usd": pos["entry_price_usd"],
                "exit_price_usd": price, "return_pct": ret * 100, "reason": exit_reason,
                "closed_at": cycle_start.isoformat(),
            })
            actions.append({"type": "sell", "symbol": pos["symbol"], "altname": altname, "reason": exit_reason, "return_pct": ret * 100})
            del state["positions"][altname]

    # ---- consider new entries ----
    if allow_new_entries:
        open_slots = args.max_concurrent_positions - len(state["positions"])
        emerging_open = sum(1 for p in state["positions"].values() if p.get("tier") == "emerging")
        for altname, result in eligible.items():
            if open_slots <= 0:
                break
            if altname in state["positions"]:
                continue
            price = prices.get(altname)
            if price is None:
                continue
            tier = result["tier"]
            if tier == "emerging" and emerging_open >= args.max_emerging_tier_positions:
                continue
            closes = get_price_history_closes(altname, args.history_days)
            time.sleep(args.request_delay)
            signal = compute_signal(closes) if closes else None
            if signal != "buy" and closes is not None:
                continue  # only trade no-history pairs opportunistically-small; require an actual buy signal when history exists
            size_usd = size_position(tier, port_value, closes, args)
            if size_usd < args.min_trade_usd or size_usd > state["cash_usd"]:
                continue
            projected_heat = portfolio_heat_pct(state, prices, args.stop_loss_pct) + (size_usd * args.stop_loss_pct / port_value if port_value else 0)
            if projected_heat > args.max_portfolio_heat_pct:
                continue
            fill_price = price * (1 + args.slippage_bps / 10_000)
            quantity = (size_usd * (1 - args.fee_bps / 10_000)) / fill_price
            state["cash_usd"] -= size_usd
            state["positions"][altname] = {
                "symbol": result.get("base", altname), "quantity": quantity, "entry_price_usd": fill_price,
                "entry_time": cycle_start.isoformat(), "tier": tier, "peak_price_usd": fill_price,
            }
            actions.append({"type": "buy", "symbol": result.get("base", altname), "altname": altname, "tier": tier,
                             "size_usd": size_usd, "fill_price": fill_price,
                             "signal_basis": "buy signal" if closes else "no-history minimum-size entry"})
            open_slots -= 1
            if tier == "emerging":
                emerging_open += 1

    final_prices = dict(prices)
    for altname in state["positions"]:
        if altname not in final_prices:
            final_prices[altname] = current_price(altname) or prices.get(altname, 0)
    final_value = portfolio_value_usd(state, final_prices)
    state["cycles_run"] += 1
    save_state(state)

    append_journal({
        "timestamp": cycle_start.isoformat(),
        "type": "cycle",
        "portfolio_value_usd_before": port_value,
        "portfolio_value_usd_after": final_value,
        "regime_allows_new_entries": allow_new_entries,
        "discovery": {"evaluated": len(candidates), "eligible": len(eligible), "rejected": rejected_count},
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
        min_24h_quote_volume_usd=args.min_24h_quote_volume_usd,
        max_spread_bps=args.max_spread_bps,
        min_price_usd=0.000001,
        blue_chip_min_volume_usd=args.blue_chip_min_volume_usd,
        blue_chip_max_spread_bps=args.blue_chip_max_spread_bps,
        established_min_volume_usd=args.established_min_volume_usd,
        established_max_spread_bps=args.established_max_spread_bps,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="Wipe paper state and restart at --starting-capital-usd")
    ap.add_argument("--starting-capital-usd", type=float, default=500.0)
    ap.add_argument("--max-candidates", type=int, default=40)
    ap.add_argument("--request-delay", type=float, default=0.4)
    ap.add_argument("--history-days", type=int, default=90,
                     help="90 days gives sma_crossover's slow=30 SMA and adaptive_ensemble's trend-following legs "
                          "room to actually signal, without running into Kraken's 720-daily-candle OHLC cap.")
    # mirrors config/risk.yaml -- keep in sync by hand, same convention as research/discover_candidates.py
    ap.add_argument("--max-position-fraction", dest="max_position_fraction", type=float, default=0.30)
    ap.add_argument("--max-position-usd", dest="max_position_usd", type=float, default=200.0)
    ap.add_argument("--min-trade-usd", dest="min_trade_usd", type=float, default=10.0)
    ap.add_argument("--max-concurrent-positions", dest="max_concurrent_positions", type=int, default=3)
    ap.add_argument("--max-emerging-tier-positions", dest="max_emerging_tier_positions", type=int, default=2)
    ap.add_argument("--target-daily-volatility-pct", dest="target_daily_volatility_pct", type=float, default=3.0)
    ap.add_argument("--volatility-size-min-mult", dest="volatility_size_min_mult", type=float, default=0.5)
    ap.add_argument("--volatility-size-max-mult", dest="volatility_size_max_mult", type=float, default=1.5)
    ap.add_argument("--max-portfolio-heat-pct", dest="max_portfolio_heat_pct", type=float, default=0.12)
    ap.add_argument("--stop-loss-pct", dest="stop_loss_pct", type=float, default=0.15)
    ap.add_argument("--take-profit-pct", dest="take_profit_pct", type=float, default=0.35)
    ap.add_argument("--trailing-stop-pct", dest="trailing_stop_pct", type=float, default=0.12)
    ap.add_argument("--circuit-breaker-floor-usd", dest="circuit_breaker_floor_usd", type=float, default=200.0)
    ap.add_argument("--circuit-breaker-daily-loss-pct", dest="circuit_breaker_daily_loss_pct", type=float, default=0.25)
    ap.add_argument("--regime-reference-pair", dest="regime_reference_pair", default="XBTUSD")
    ap.add_argument("--regime-sma-window-days", dest="regime_sma_window_days", type=int, default=30)
    ap.add_argument("--fee-bps", dest="fee_bps", type=float, default=40.0)
    ap.add_argument("--slippage-bps", dest="slippage_bps", type=float, default=20.0)
    # discovery safety thresholds -- mirrors config/discovery.yaml
    ap.add_argument("--min-24h-quote-volume-usd", dest="min_24h_quote_volume_usd", type=float, default=500_000)
    ap.add_argument("--max-spread-bps", dest="max_spread_bps", type=float, default=50)
    ap.add_argument("--blue-chip-min-volume-usd", dest="blue_chip_min_volume_usd", type=float, default=100_000_000)
    ap.add_argument("--blue-chip-max-spread-bps", dest="blue_chip_max_spread_bps", type=float, default=10)
    ap.add_argument("--established-min-volume-usd", dest="established_min_volume_usd", type=float, default=10_000_000)
    ap.add_argument("--established-max-spread-bps", dest="established_max_spread_bps", type=float, default=25)
    args = ap.parse_args()
    run_cycle(args)


if __name__ == "__main__":
    main()
