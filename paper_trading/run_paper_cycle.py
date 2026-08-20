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

# See research/discover_candidates.py for why: discovered symbols can contain
# Unicode a narrow Windows console codepage can't print, which otherwise crashes
# an autonomous cycle on a print() after all the real work is already done.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from research import discover_candidates as disco  # noqa: E402
from backtest import fetch_history as fh  # noqa: E402
from backtest import strategies as strat  # noqa: E402

STATE_PATH = REPO_ROOT / "state" / "paper_portfolio.json"
JOURNAL_PATH = REPO_ROOT / "journal" / "paper_trades.jsonl"
REGIME_CACHE_PATH = REPO_ROOT / "state" / "regime_cache.json"
REGIME_CACHE_TTL_SECONDS = 3600  # regime is a daily-scale (30d SMA) signal -- refetching every 15min cycle is
                                  # unnecessary load on CoinGecko's free tier for no real freshness gain
PRICE_HISTORY_CACHE_DIR = REPO_ROOT / "state" / "price_history_cache"
PRICE_HISTORY_CACHE_TTL_SECONDS = 3600  # same reasoning as the regime cache: a mint's daily closes barely
                                          # change within 15 minutes, but signal generation re-fetches every
                                          # eligible candidate's history every cycle -- the dominant source of
                                          # CoinGecko rate-limit backoff once discovery finds several candidates
DISCOVERY_ROTATION_PATH = REPO_ROOT / "state" / "discovery_rotation.json"
DISCOVERY_ROTATION_HISTORY_CYCLES = 3  # remember roughly this many cycles' worth of evaluated mints -- a
                                         # bounded, FIFO "recently seen" window, not permanent exclusion, so a
                                         # token drops back into rotation once enough cycles have passed

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


# ---- Discovery rotation --------------------------------------------------------
# gather_candidates() returns more unique mints than max_candidates can afford to
# fully evaluate (RugCheck is a real, rate-limited cost per stage-1 survivor) --
# e.g. a live run found 63 unique candidates against a cap of 40. Naively taking
# the first max_candidates in gather_candidates()'s fixed source-priority order
# means the same ~23 candidates at the tail NEVER get evaluated, cycle after
# cycle, since Jupiter's organic/trending lists don't reshuffle drastically
# within 15 minutes. This rotates which candidates get the cap's worth of
# evaluation slots across cycles instead of always favoring the same head of
# the list, so real breadth (config/discovery.yaml's own stated goal) actually
# gets used session-wide, not just in any one cycle's snapshot.

def _load_recently_evaluated() -> set[str]:
    if not DISCOVERY_ROTATION_PATH.exists():
        return set()
    try:
        data = json.loads(DISCOVERY_ROTATION_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    return set(data.get("recently_evaluated_ordered", []))


def _save_recently_evaluated(mints_this_cycle: list[str], max_candidates: int) -> None:
    prior: list[str] = []
    if DISCOVERY_ROTATION_PATH.exists():
        try:
            prior = json.loads(DISCOVERY_ROTATION_PATH.read_text()).get("recently_evaluated_ordered", [])
        except (json.JSONDecodeError, OSError):
            prior = []
    combined = prior + mints_this_cycle
    cap = max_candidates * DISCOVERY_ROTATION_HISTORY_CYCLES
    combined = combined[-cap:]  # bounded FIFO -- old entries age out, letting those tokens rotate back in
    DISCOVERY_ROTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_ROTATION_PATH.write_text(json.dumps({
        "recently_evaluated_ordered": combined,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))


def select_candidates_for_rotation(all_mints: list[str], max_candidates: int, recently_evaluated: set[str],
                                    held_mints: set[str] | None = None) -> list[str]:
    """Picks which of all_mints get this cycle's max_candidates evaluation
    slots. Any mint currently held as an open position is always included
    (doesn't count against the cap) -- an existing position must never
    silently fall out of consideration for a strategy-driven exit just
    because rotation deprioritized it (config/discovery.yaml's
    pinned_candidates documents this exact intent for open positions).
    Among the rest, mints NOT in recently_evaluated go first, so the cap's
    slots rotate across the full discovered set over multiple cycles instead
    of always going to whichever tokens happen to sort first."""
    held_mints = held_mints or set()
    held_in_pool = [m for m in all_mints if m in held_mints]
    rest = [m for m in all_mints if m not in held_mints]
    unseen = [m for m in rest if m not in recently_evaluated]
    seen = [m for m in rest if m in recently_evaluated]
    return held_in_pool + (unseen + seen)[:max_candidates]


# ---- Pricing ------------------------------------------------------------------

CHUNK_SIZE = 25  # keep each search URL a safe length; Jupiter's search accepts a comma-separated query


def current_prices(mints: list[str]) -> dict[str, float]:
    """Live USD prices for many mints in as few Jupiter search calls as
    possible -- verified live that /search?query=<mint1>,<mint2>,... returns
    all matches in one response, instead of one call per mint. This replaced
    a one-call-per-mint loop that was hitting 429s on nearly every cycle
    (N sequential requests, 0.4s apart, against a free endpoint) -- batching
    cuts that to ceil(N/25) requests per cycle. Works uniformly for core
    assets, discovered candidates, and existing positions; a mint Jupiter's
    search doesn't match (e.g. delisted/unindexed) is simply absent from the
    returned dict, same as the old per-mint version returning None for it."""
    prices: dict[str, float] = {}
    unique_mints = list(dict.fromkeys(mints))  # de-dupe, keep order
    for i in range(0, len(unique_mints), CHUNK_SIZE):
        chunk = unique_mints[i:i + CHUNK_SIZE]
        results = disco._get_json(f"{disco.JUPITER_BASE}/search?query=" + ",".join(chunk))
        chunk_set = set(chunk)
        for r in results or []:
            mid = r.get("id")
            if mid in chunk_set and r.get("usdPrice") is not None:
                prices[mid] = r["usdPrice"]
    return prices


def current_price(mint: str) -> float | None:
    """Single-mint convenience wrapper around current_prices() -- prefer
    calling current_prices() directly with a full list when pricing more
    than one mint, to get the batching benefit."""
    return current_prices([mint]).get(mint)


def portfolio_value_usd(state: dict, prices: dict[str, float]) -> float:
    total = state["cash_usd"]
    for mint, pos in state["positions"].items():
        price = prices.get(mint)
        if price is not None:
            total += pos["quantity"] * price
    return total


def all_positions_priced(state: dict, prices: dict[str, float]) -> bool:
    """False if any open position is missing a price this cycle.
    portfolio_value_usd() silently EXCLUDES unpriced positions from its sum
    -- fine for one stale/delisted token, but if pricing fails for every open
    position at once (e.g. a single batched request in current_prices() gets
    rate-limited and fails as a unit), portfolio_value_usd() quietly
    collapses toward cash-only. That looks exactly like a real crash to
    circuit-breaker/peak-tracking logic even though nothing actually moved --
    a real trip on this exact pattern computed $33.91 (cash only) against a
    $50.73 peak, while a fresh independent price check moments later showed
    the true value was $49.99. Callers must gate any circuit-breaker or
    peak-update decision on this returning True; when it's False, the
    portfolio_value_usd() for this cycle is not trustworthy for judgment
    calls, only for its already-correctly-scoped per-position uses."""
    return all(mint in prices for mint in state["positions"])


# ---- Regime filter ------------------------------------------------------------

def _load_regime_cache(reference_coin: str, sma_window_days: int) -> bool | None:
    """Returns the cached regime read if it's for the same coin/window and
    still fresh, else None (meaning: fetch live)."""
    if not REGIME_CACHE_PATH.exists():
        return None
    try:
        cached = json.loads(REGIME_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if cached.get("reference_coin") != reference_coin or cached.get("sma_window_days") != sma_window_days:
        return None
    computed_at = datetime.fromisoformat(cached["computed_at"])
    age_seconds = (datetime.now(timezone.utc) - computed_at).total_seconds()
    if age_seconds > REGIME_CACHE_TTL_SECONDS:
        return None
    return cached["allows_new_entries"]


def _save_regime_cache(reference_coin: str, sma_window_days: int, allows_new_entries: bool) -> None:
    REGIME_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGIME_CACHE_PATH.write_text(json.dumps({
        "reference_coin": reference_coin,
        "sma_window_days": sma_window_days,
        "allows_new_entries": allows_new_entries,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }))


def regime_allows_new_entries(reference_coin: str, sma_window_days: int) -> bool:
    """A 30-day SMA doesn't meaningfully change within a 15-minute paper-cycle
    interval, so this is cached for REGIME_CACHE_TTL_SECONDS instead of hit
    live every cycle -- cuts CoinGecko calls roughly 4x (once/hour instead of
    once/15min) with no real loss of decision quality, and reduces the 429
    backoff delays that were adding ~60s to nearly every cycle."""
    cached = _load_regime_cache(reference_coin, sma_window_days)
    if cached is not None:
        return cached
    try:
        payload = fh.fetch_market_chart(reference_coin, days=sma_window_days + 5)
    except Exception as e:
        print(f"  ! regime filter data unavailable ({e}) -- defaulting to conservative (no new entries)", file=sys.stderr)
        return False
    closes = [p for _, p in payload.get("prices", [])]
    if len(closes) < sma_window_days:
        return False
    sma = sum(closes[-sma_window_days:]) / sma_window_days
    result = closes[-1] > sma
    _save_regime_cache(reference_coin, sma_window_days, result)
    return result


# ---- Signal generation --------------------------------------------------------

def _price_history_cache_path(mint: str, days: int) -> Path:
    return PRICE_HISTORY_CACHE_DIR / f"{mint}_{days}d.json"


def _load_price_history_cache(mint: str, days: int) -> list[float] | None:
    path = _price_history_cache_path(mint, days)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    computed_at = datetime.fromisoformat(cached["computed_at"])
    if (datetime.now(timezone.utc) - computed_at).total_seconds() > PRICE_HISTORY_CACHE_TTL_SECONDS:
        return None
    return cached["closes"]


def _save_price_history_cache(mint: str, days: int, closes: list[float]) -> None:
    PRICE_HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _price_history_cache_path(mint, days).write_text(json.dumps({
        "closes": closes,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }))


def get_price_history_closes(mint: str, days: int) -> list[float] | None:
    """Cached for PRICE_HISTORY_CACHE_TTL_SECONDS per (mint, days) -- with
    several eligible candidates per cycle, this was the dominant source of
    CoinGecko 429 backoff delay (one uncached fetch per candidate, every
    15-minute cycle, for daily closes that don't meaningfully change that
    often). A stale/corrupt cache entry or a genuinely new mint just falls
    through to a live fetch, same as an empty cache."""
    cached = _load_price_history_cache(mint, days)
    if cached is not None:
        return cached
    try:
        payload = fh.fetch_market_chart_by_contract(mint, days=days)
    except Exception:
        return None
    prices = payload.get("prices") or []
    if len(prices) < 15:  # not enough for even the fastest indicator to warm up meaningfully
        return None
    closes = [p for _, p in prices]
    _save_price_history_cache(mint, days, closes)
    return closes


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
    recently_evaluated = _load_recently_evaluated()
    mints = select_candidates_for_rotation(list(candidates.keys()), args.max_candidates, recently_evaluated,
                                            held_mints=set(state["positions"]))
    eligible: dict[str, dict] = {}
    rejected_count = 0
    for mint in mints:
        result = disco.evaluate_candidate(mint, candidates[mint], disco_args(args))
        if result["eligible"]:
            eligible[mint] = result
        else:
            rejected_count += 1
    _save_recently_evaluated(mints, args.max_candidates)
    print(f"Discovery: {len(candidates)} found, {len(mints)} evaluated, {len(eligible)} eligible, {rejected_count} rejected.")

    # include existing paper positions even if they fell out of discovery this cycle
    tracked_mints = set(eligible) | set(state["positions"])
    for mint in disco.CORE_ASSET_MINTS:
        tracked_mints.discard(mint)

    # Observed live: the pricing call below hit a 429 on nearly every single
    # cycle, always recovered by the retry, but at a consistent ~12s backoff
    # tax each time. gather_candidates() just made 5 back-to-back Jupiter
    # calls (2 organic + 2 trending + 1 recent intervals) with no spacing --
    # a brief pause here gives Jupiter's rate-limit window a chance to
    # settle before asking it for prices, instead of relying on the retry to
    # clean up every cycle.
    time.sleep(2.0)
    prices = current_prices(list(tracked_mints | disco.CORE_ASSET_MINTS))

    # If the initial batched fetch missed any OPEN position specifically,
    # retry just those before anything downstream (circuit breaker, and
    # crucially the exit-management stop-loss/take-profit/trailing-stop
    # checks below) runs against incomplete data. This closed a real gap:
    # previously only the end-of-cycle report/journal got a fallback
    # re-fetch, so a cycle whose initial pricing failed would silently skip
    # the stop-loss check entirely for that cycle even though a price WAS
    # obtainable (proven by that same later fallback succeeding) -- exactly
    # the kind of gap that matters most when a position is actually
    # approaching its stop-loss, not a hypothetical edge case.
    unpriced_positions = [mint for mint in state["positions"] if mint not in prices]
    if unpriced_positions:
        prices.update(current_prices(unpriced_positions))

    port_value = portfolio_value_usd(state, prices)
    port_value_reliable = all_positions_priced(state, prices)

    if not port_value_reliable:
        unpriced = [mint for mint in state["positions"] if mint not in prices]
        print(f"  ! {len(unpriced)}/{len(state['positions'])} open position(s) still couldn't be priced this "
              f"cycle after a retry -- skipping circuit breaker check, peak update, AND this cycle's stop-loss/"
              f"take-profit check for the affected position(s) (no reliable price to check them against)", file=sys.stderr)
    else:
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
    # not_traded records why each eligible candidate that WASN'T bought this
    # cycle was skipped -- without this, the journal only ever shows the
    # trades that happened, not the reasoning behind the (usually much more
    # common) decision not to trade one. That's a real audit gap: "why didn't
    # we buy X this cycle" was previously unanswerable after the fact without
    # re-deriving it from scratch.
    not_traded: dict[str, str] = {}
    if not allow_new_entries:
        not_traded = {mint: "regime filter blocking new entries (risk-OFF)"
                      for mint in eligible if mint not in state["positions"]}
    if allow_new_entries:
        open_slots = args.max_concurrent_positions - len(state["positions"])
        emerging_open = sum(1 for p in state["positions"].values() if p.get("tier") == "emerging")
        for mint, result in eligible.items():
            if mint in state["positions"]:
                continue  # already held -- not a "skip", just not a new entry decision
            if open_slots <= 0:
                not_traded[mint] = f"no open slots (max_concurrent_positions={args.max_concurrent_positions})"
                continue
            price = prices.get(mint)
            if price is None:
                not_traded[mint] = "no live price available"
                continue
            tier = result["tier"]
            if tier == "emerging" and emerging_open >= args.max_emerging_tier_positions:
                not_traded[mint] = f"emerging-tier cap reached (max_emerging_tier_positions={args.max_emerging_tier_positions})"
                continue
            closes = get_price_history_closes(mint, args.history_days)
            time.sleep(args.request_delay)
            signal = compute_signal(closes) if closes else None
            if signal != "buy" and closes is not None:
                not_traded[mint] = f"strategy signal was '{signal}', not buy"
                continue  # only trade no-history tokens opportunistically-small; require an actual buy signal when history exists
            size_usd = size_position(tier, port_value, closes, args)
            if size_usd < args.min_trade_usd:
                not_traded[mint] = f"sized position ${size_usd:.2f} below min_trade_usd (${args.min_trade_usd:.2f})"
                continue
            if size_usd > state["cash_usd"]:
                not_traded[mint] = f"sized position ${size_usd:.2f} exceeds available cash (${state['cash_usd']:.2f})"
                continue
            projected_heat = portfolio_heat_pct(state, prices, args.stop_loss_pct) + (size_usd * args.stop_loss_pct / port_value if port_value else 0)
            if projected_heat > args.max_portfolio_heat_pct:
                not_traded[mint] = f"would exceed max_portfolio_heat_pct ({projected_heat:.1%} > {args.max_portfolio_heat_pct:.1%})"
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
    missing = [mint for mint in state["positions"] if mint not in final_prices]
    if missing:
        final_prices.update(current_prices(missing))
        for mint in missing:
            final_prices.setdefault(mint, prices.get(mint, 0))
    final_value = portfolio_value_usd(state, final_prices)
    state["cycles_run"] += 1
    save_state(state)

    # If the initial pricing pass was incomplete, port_value understated the
    # real portfolio (same root cause as the circuit-breaker guard above) --
    # don't let that same misleading number leak into the reported/journaled
    # "before" value now that final_prices likely recovered it via its own
    # re-fetch. Report it as the best available estimate instead of a number
    # that reads like a same-cycle crash-and-recover that never happened.
    reported_before = final_value if not port_value_reliable else port_value

    append_journal({
        "timestamp": cycle_start.isoformat(),
        "type": "cycle",
        "portfolio_value_usd_before": reported_before,
        "portfolio_value_usd_before_pricing_incomplete": not port_value_reliable,
        "portfolio_value_usd_after": final_value,
        "regime_allows_new_entries": allow_new_entries,
        "discovery": {"found": len(candidates), "evaluated": len(mints), "eligible": len(eligible), "rejected": rejected_count},
        "actions": actions,
        "not_traded": {mint: {"symbol": eligible[mint]["symbol"], "reason": reason} for mint, reason in not_traded.items()},
        "open_positions": len(state["positions"]),
    })

    print(f"\nActions this cycle: {len(actions)}")
    for a in actions:
        if a["type"] == "buy":
            print(f"  BUY  {a['symbol']:>10}  ${a['size_usd']:.2f}  tier={a['tier']:<10}  ({a['signal_basis']})")
        else:
            print(f"  SELL {a['symbol']:>10}  {a['reason']}  return={a['return_pct']:+.1f}%")
    if not_traded:
        print(f"\nEligible but not traded ({len(not_traded)}):")
        for mint, reason in not_traded.items():
            print(f"  {eligible[mint]['symbol']:>10}  {reason}")
    before_note = "  (pricing was incomplete at cycle start -- showing best available estimate, not a same-cycle move)" if not port_value_reliable else ""
    print(f"\nPortfolio value: ${reported_before:.2f} -> ${final_value:.2f}{before_note}  "
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
    ap.add_argument("--max-candidates", type=int, default=40)
    ap.add_argument("--limit-per-source", type=int, default=15)
    ap.add_argument("--request-delay", type=float, default=0.4)
    ap.add_argument("--history-days", type=int, default=90,
                     help="Was 30 -- raised after fixing backtest/fetch_history.py's daily-resampling bug. With "
                          "genuinely daily bars (previously days<=30 silently returned hourly data from "
                          "CoinGecko), 30 days barely lets sma_crossover's slow=30 SMA compute once, let alone "
                          "show a real crossover -- adaptive_ensemble's trend-following legs would almost never "
                          "fire. 90 days gives them room to actually signal.")
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
    ap.add_argument("--max-top-holder-pct", dest="max_top_holder_pct", type=float, default=22.0)
    ap.add_argument("--blue-chip-mcap-usd", dest="blue_chip_mcap_usd", type=float, default=50_000_000)
    ap.add_argument("--blue-chip-holder-count", dest="blue_chip_holder_count", type=int, default=10_000)
    ap.add_argument("--established-mcap-usd", dest="established_mcap_usd", type=float, default=5_000_000)
    ap.add_argument("--established-holder-count", dest="established_holder_count", type=int, default=2_000)
    args = ap.parse_args()
    run_cycle(args)


if __name__ == "__main__":
    main()
