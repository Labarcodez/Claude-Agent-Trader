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
  - No max_daily_trade_count / max_daily_volume_usd cadence caps -- this
    script is typically run manually or via /loop at a deliberate interval,
    so cadence is controlled by how often you run it. Two same-token
    re-entry guards DO exist, both real observed gaps: a mint sold this
    cycle can't be bought back in the SAME cycle (a take-profit exit and an
    immediate same-price re-entry would otherwise cancel out the exit's
    purpose), and a mint stopped out via stop-loss can't be re-bought for
    --min-hours-between-trades-same-token (default 4h, matches
    config/risk.yaml) -- see recently_stopped_out(). The stop-loss cooldown
    was added after running two independent loops (this session's cron +
    a local terminal loop) against the same state made actual cadence
    between cycles faster than either loop's own interval, letting a token
    whipsaw a stop-loss twice within ~2 hours.
  - Core position sizing, tiering, volatility scaling, portfolio heat,
    regime filter, and stop-loss/take-profit/trailing-stop ARE all real,
    reusing the exact same code (backtest/strategies.py, research/discover_candidates.py)
    the live skill is documented to use.

Scout tier (PAPER-TRADING EXPERIMENTAL, see scout_disco_args()): a second,
looser discovery pass for tokens too new/illiquid to pass the normal
thresholds, entered small (--scout-position-fraction, default 20% of the
tier-target size) and scaled to full size only if price rises
--scale-in-price-threshold-pct from entry ("price action confirms
momentum" -- volume confirmation isn't implemented, no reliable live volume
signal exists in this pipeline once a position is open). Sells enough to
recoup 100% of cost basis once value reaches --profit-take-multiple (default
2.0x), letting the remainder ride with zero capital still at risk, on a
tighter --scout-trailing-stop-pct (default 8%) than other tiers get
pre-profit-take -- "chase highs up and sell as soon as they go down."
Capped in
aggregate by --max-memecoin-exposure-fraction (default 20% of portfolio,
scout+emerging combined -- was 5% at the user's original request, raised
after a single pre-existing emerging position alone exceeded that budget
and blocked every scout entry indefinitely). Does NOT touch config/discovery.yaml or
config/risk.yaml -- those stay the real safety boundary for anything live
(CLAUDE.md rule 7); this is how a strategy like this gets a real paper track
record before that conversation ever happens. Disable with --disable-scout-tier.

Usage:
    python3 paper_trading/run_paper_cycle.py
    python3 paper_trading/run_paper_cycle.py --reset       # wipe paper state, restart at starting capital
    python3 paper_trading/run_paper_cycle.py --max-candidates 10
"""
from __future__ import annotations
import argparse
import json
import os
import random
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
from scripts.cycle_lock import CycleLock  # noqa: E402

STATE_PATH = REPO_ROOT / "state" / "paper_portfolio.json"
JOURNAL_PATH = REPO_ROOT / "journal" / "paper_trades.jsonl"
LOCK_PATH = REPO_ROOT / "state" / "paper_cycle.lock"
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
MAX_FRESH_PRICE_HISTORY_FETCHES_PER_CYCLE = 7  # discovery now rotates through a ~2,600-token pool, so most
                                                 # eligible candidates each cycle are price_history_cache misses
                                                 # (never seen before). A single fetch's worst case is ~100s
                                                 # (backtest/fetch_history.py's own retry policy: 4 attempts,
                                                 # 10/20/30/40s backoff) -- observed live, even just 5-6 fresh
                                                 # fetches in one cycle repeatedly cost 1m40s-1m50s, not the
                                                 # ~13s typical. Was 8, then lowered to 5 over cycle-overlap risk
                                                 # against what was then a 15-minute loop interval. Raised back
                                                 # up (not all the way -- 6, then reconsidered to 7) now that
                                                 # two things changed: CycleLock (see that class) means an
                                                 # overrunning cycle now just makes the next scheduled run wait
                                                 # for the lock instead of corrupting shared state, and
                                                 # max_concurrent_positions going 10->15 means more slots need
                                                 # filling with genuinely fresh candidates per cycle to actually
                                                 # get used rather than sitting open. Typical observed cycle
                                                 # time stayed ~60-90s even before this change, well under the
                                                 # current 5-minute interval -- the ~100s/fetch figure above is
                                                 # a rare worst case, not the norm. Deferred candidates are
                                                 # simply reconsidered next cycle either way, no correctness loss.

TIER_MULTIPLIERS = {"blue_chip": 1.0, "established": 0.7, "emerging": 0.4, "scout": 0.4}
MEMECOIN_TIERS = {"scout", "emerging"}  # what counts toward max_memecoin_exposure_fraction


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


# CycleLock now lives in scripts/cycle_lock.py (imported above) -- extracted
# so the live trade-cycle skill can guard its own state files (
# state/starting_capital.json, state/circuit_breaker.json,
# journal/trades.jsonl) against the exact same concurrent-process race this
# was originally built for, now with real money instead of paper.


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

    Among the rest, mints NOT in recently_evaluated go first -- but shuffled,
    not taken in gather_candidates()'s fixed source order. Without the
    shuffle, this silently reproduced the exact bug it was built to fix:
    gather_candidates() lists momentum sources (organic/trending/traded/
    recent, ~70-130 tokens combined) before the verified-tag source
    (~2,561 tokens). That small momentum pool cycles fast enough to supply
    max_candidates' worth of "unseen" tokens on its own almost every cycle,
    so the selection never had to reach past index ~130 -- verified live: 43
    selected candidates in one real cycle, every single one from position
    0-126 of a 2,596-token pool. The 2,561-token verified tail was
    "discovered" but never actually evaluated, cycle after cycle. Shuffling
    unseen (and seen, for the fallback case) means every mint in the pool
    gets a fair chance at a slot regardless of which source found it or
    where gather_candidates() happened to place it."""
    held_mints = held_mints or set()
    held_in_pool = [m for m in all_mints if m in held_mints]
    rest = [m for m in all_mints if m not in held_mints]
    unseen = [m for m in rest if m not in recently_evaluated]
    seen = [m for m in rest if m in recently_evaluated]
    random.shuffle(unseen)
    random.shuffle(seen)
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
    still fresh, else None (meaning: fetch live). Fail-safe on ANY parse
    problem -- a cache file that's valid JSON but missing/malformed fields
    (partial write, disk issue, manual edit) must degrade to a live fetch,
    not crash the whole cycle. Verified live: a cache missing computed_at
    raised an uncaught KeyError here before this guard existed."""
    if not REGIME_CACHE_PATH.exists():
        return None
    try:
        cached = json.loads(REGIME_CACHE_PATH.read_text())
        if cached.get("reference_coin") != reference_coin or cached.get("sma_window_days") != sma_window_days:
            return None
        computed_at = datetime.fromisoformat(cached["computed_at"])
        age_seconds = (datetime.now(timezone.utc) - computed_at).total_seconds()
        if age_seconds > REGIME_CACHE_TTL_SECONDS:
            return None
        return cached["allows_new_entries"]
    except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
        return None


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
    """Fail-safe on ANY parse problem, not just JSON/OS errors -- see
    _load_regime_cache()'s docstring for why a malformed-but-valid-JSON cache
    file must degrade to None (triggering a live fetch) rather than crash."""
    path = _price_history_cache_path(mint, days)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text())
        computed_at = datetime.fromisoformat(cached["computed_at"])
        if (datetime.now(timezone.utc) - computed_at).total_seconds() > PRICE_HISTORY_CACHE_TTL_SECONDS:
            return None
        return cached["closes"]
    except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
        return None


def _save_price_history_cache(mint: str, days: int, closes: list[float]) -> None:
    PRICE_HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _price_history_cache_path(mint, days).write_text(json.dumps({
        "closes": closes,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }))


def get_price_history_closes(mint: str, days: int, cached: list[float] | None = None) -> list[float] | None:
    """Cached for PRICE_HISTORY_CACHE_TTL_SECONDS per (mint, days) -- with
    several eligible candidates per cycle, this was the dominant source of
    CoinGecko 429 backoff delay (one uncached fetch per candidate, every
    15-minute cycle, for daily closes that don't meaningfully change that
    often). A stale/corrupt cache entry or a genuinely new mint just falls
    through to a live fetch, same as an empty cache.

    Pass `cached` if the caller already did its own _load_price_history_cache()
    lookup (e.g. to decide whether this fetch counts against a per-cycle
    budget) -- avoids re-reading and re-parsing the same cache file a second
    time for no reason. Leave it None to have this function do its own
    lookup as usual.

    Uses an impatient retry policy (2 attempts, 3s base wait -- ~9s worst
    case) instead of fetch_history.py's default (~100s worst case): this
    runs inside a tight 15-minute cron loop where MAX_FRESH_PRICE_HISTORY_
    FETCHES_PER_CYCLE candidates each hitting the patient default could
    still add up to several minutes even after that cap -- observed live,
    repeatedly. A candidate this gives up on quickly just gets reconsidered
    next cycle (discovery rotation persists), so failing fast costs nothing
    but a delay, unlike a one-off backtest run where the default patience is
    the right call."""
    if cached is None:
        cached = _load_price_history_cache(mint, days)
    if cached is not None:
        return cached
    try:
        payload = fh.fetch_market_chart_by_contract(mint, days=days, retries=2, base_wait=3.0)
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


def compute_partial_profit_take(cost_basis_usd: float, quantity: float, price: float,
                                 profit_take_multiple: float) -> float | None:
    """Returns the quantity to sell to recoup exactly cost_basis_usd once the
    position's current value reaches profit_take_multiple x cost_basis_usd,
    or None if not triggered (or there's no cost basis left to recoup --
    already taken, or a pre-existing position with no tracked cost basis).
    Selling exactly enough to recoup the original stake turns the remaining
    quantity into a risk-free "let it ride" runner: even if it goes to zero
    from here, the original investment is already banked. This is the
    "sell 100% of initial investment after a 2x-3x gain" rule -- applied to
    scout-tier positions only (see run_cycle's exit-management loop)."""
    if cost_basis_usd <= 0:
        return None
    current_value = quantity * price
    if current_value < cost_basis_usd * profit_take_multiple:
        return None
    return min(cost_basis_usd / price, quantity)  # never sell more than we hold


def compute_scale_in_topup(cost_basis_usd: float, full_target_size: float, cash_usd: float,
                            exposure_room_usd: float, min_trade_usd: float) -> tuple[float, str | None]:
    """Pure sizing decision for a scout position's momentum-confirmed scale-in,
    split out of run_cycle's scale-in loop so it's unit-testable without
    mocking the whole cycle (mirrors compute_partial_profit_take above).
    Returns (amount_to_add_usd, skip_reason): skip_reason is None when a
    top-up -- possibly smaller than the full gap to full_target_size, capped
    to whatever fits under cash/exposure room -- should proceed;
    amount_to_add_usd is 0.0 whenever skip_reason is set. Caps to available
    room rather than an all-or-nothing skip: a position that legitimately
    confirmed momentum shouldn't get stuck forever just because the FULL
    top-up doesn't fit under the exposure cap while partial room exists."""
    additional_needed = full_target_size - cost_basis_usd
    if additional_needed < min_trade_usd:
        return 0.0, "already_full"
    capped_needed = min(additional_needed, cash_usd, max(exposure_room_usd, 0.0))
    if capped_needed < min_trade_usd:
        return 0.0, "insufficient_room"
    return capped_needed, None


def recently_stopped_out(closed_trades: list[dict], mint: str, now: datetime, cooldown_hours: float) -> bool:
    """True if `mint` was closed via a stop-loss within the last
    cooldown_hours -- mirrors config/risk.yaml's
    min_hours_between_trades_same_token (4h), the same cadence cap the live
    trade-cycle skill enforces. Only a stop-loss exit triggers this: a
    take-profit or trailing-stop exit is a good outcome, and immediate
    same-cycle re-entry after any exit is already blocked separately by
    sold_this_cycle -- this covers the multi-cycle gap that leaves.

    This module's docstring documents skipping a real multi-cycle cooldown
    as deliberate ('cadence is controlled by how often you run it'), which
    held while only one process ran cycles. Mining the journal found it
    doesn't hold once two independent loops (this session's cron + a local
    terminal loop) both run cycles against the same state: CEZ was stopped
    out at -29.6%, then re-bought only ~2 hours later (under the configured
    4h) and immediately stopped out again at -15.3% -- the loops' combined
    cadence was faster than either loop's own interval. A stop-loss cooldown
    is risk-reducing, not risk-adding, so it's a safe default to add rather
    than something that needs the exposure-cap-style live risk sign-off."""
    for trade in closed_trades:
        if trade.get("mint") != mint or "stop-loss" not in (trade.get("reason") or ""):
            continue
        try:
            closed_at = datetime.fromisoformat(trade["closed_at"])
        except (KeyError, ValueError, TypeError):
            continue
        if (now - closed_at).total_seconds() < cooldown_hours * 3600:
            return True
    return False


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
        no_organic=False, no_trending=False, no_traded=False, no_recent=False, no_verified=False,
        limit_per_source=args.limit_per_source,
    ))
    recently_evaluated = _load_recently_evaluated()
    mints = select_candidates_for_rotation(list(candidates.keys()), args.max_candidates, recently_evaluated,
                                            held_mints=set(state["positions"]))
    eligible: dict[str, dict] = {}
    rejected_count = 0
    scout_count = 0
    for mint in mints:
        result = disco.evaluate_candidate(mint, candidates[mint], disco_args(args))
        if result["eligible"]:
            eligible[mint] = result
            continue
        # Failed the normal (established-token-shaped) thresholds -- try the
        # looser scout thresholds before giving up on it. Only worth the
        # extra evaluation for candidates that didn't already qualify
        # normally; a normal-eligible candidate is never re-checked as scout.
        if args.enable_scout_tier:
            scout_result = disco.evaluate_candidate(mint, candidates[mint], scout_disco_args(args))
            if scout_result["eligible"]:
                scout_result["tier"] = "scout"
                eligible[mint] = scout_result
                scout_count += 1
                continue
        rejected_count += 1
    _save_recently_evaluated(mints, args.max_candidates)
    print(f"Discovery: {len(candidates)} found, {len(mints)} evaluated, {len(eligible)} eligible "
          f"({scout_count} scout-tier), {rejected_count} rejected.")

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
    sold_this_cycle: set[str] = set()

    # ---- manage existing positions first (exits are never regime-gated) ----
    for mint, pos in list(state["positions"].items()):
        price = prices.get(mint)
        if price is None:
            continue
        pos["peak_price_usd"] = max(pos.get("peak_price_usd", pos["entry_price_usd"]), price)

        # Partial profit-take (scout tier only): "sell 100% of initial
        # investment after a 2x-3x gain, let the remainder ride." Checked
        # before the full-exit logic below, and skips it for this cycle if it
        # fires -- a partial sell isn't a full exit, and re-evaluating a full
        # stop-loss/take-profit against the just-reduced position in the same
        # pass would use stale peak/ret bookkeeping from before the trim.
        if pos.get("tier") == "scout" and not pos.get("profit_taken"):
            sell_qty = compute_partial_profit_take(pos.get("cost_basis_usd", 0.0), pos["quantity"], price,
                                                     args.profit_take_multiple)
            if sell_qty is not None:
                proceeds = sell_qty * price * (1 - args.fee_bps / 10_000 - args.slippage_bps / 10_000)
                state["cash_usd"] += proceeds
                pos["quantity"] -= sell_qty
                pos["cost_basis_usd"] = 0.0  # fully recouped -- the remainder is risk-free from here
                pos["profit_taken"] = True
                actions.append({"type": "partial_sell", "symbol": pos["symbol"], "mint": mint,
                                 "reason": f"profit-take ({args.profit_take_multiple:.1f}x cost basis)",
                                 "quantity_sold": sell_qty, "proceeds_usd": proceeds,
                                 "remaining_quantity": pos["quantity"]})
                continue

        ret = (price / pos["entry_price_usd"]) - 1
        drawdown_from_peak = (price / pos["peak_price_usd"]) - 1
        exit_reason = None
        if pos.get("profit_taken"):
            # "Let the remaining tokens ride for upside potential without
            # risking your own capital." cost_basis_usd is already 0 -- a
            # traditional stop-loss measured against the original entry no
            # longer protects real capital (there's nothing left to lose),
            # so it no longer applies here. What DOES still make sense is
            # protecting the accumulated paper gains via a trailing stop
            # against this runner's own peak -- just a wider one than a
            # normal position gets, specifically so routine volatility on a
            # position with nothing left to lose doesn't chop off the exact
            # upside this whole strategy exists to capture.
            if drawdown_from_peak <= -args.house_money_trailing_stop_pct:
                exit_reason = f"house-money trailing-stop ({drawdown_from_peak:+.1%} from peak)"
        else:
            # Scout tier skips the blanket take_profit_pct exit (35% by
            # default) -- the whole point of the tiered strategy is letting
            # a scout position run to a 2x-3x gain via the partial
            # profit-take above instead of the full position closing out at
            # +35% before it ever gets there.
            take_profit_threshold = None if pos.get("tier") == "scout" else args.take_profit_pct
            # Scout tier chases with a tighter trailing stop than other
            # tiers -- explicit user request: "chase highs up and sell as
            # soon as they go down" for these higher-risk, low-dollar
            # positions specifically, without touching the wider stop that's
            # been working well for established/emerging/blue_chip (CATE,
            # established tier, is 4/4 live). Only applies pre-profit-take;
            # once profit_taken, the position uses house_money_trailing_stop_pct
            # (wider, 30%) instead, so a proven winner still gets room to run.
            trailing_stop_threshold = args.scout_trailing_stop_pct if pos.get("tier") == "scout" else args.trailing_stop_pct
            if ret <= -args.stop_loss_pct:
                exit_reason = f"stop-loss ({ret:+.1%})"
            elif take_profit_threshold is not None and ret >= take_profit_threshold:
                exit_reason = f"take-profit ({ret:+.1%})"
            elif ret > 0 and drawdown_from_peak <= -trailing_stop_threshold:
                exit_reason = f"trailing-stop ({drawdown_from_peak:+.1%} from peak)"
        if exit_reason is None and not pos.get("profit_taken"):
            # Deliberately NOT gated on `mint in eligible` (a real gap this
            # had since the original implementation, uncommented and
            # unexplained): eligibility is a discovery-time safety gate
            # (liquidity, holder count, organic score, top-holder
            # concentration...) about whether it's safe to newly BUY a
            # token, not about its price trend -- and it's exactly the kind
            # of thing that fluctuates cycle to cycle for the thin/new
            # tokens scout tier holds. Gating the sell-signal check on it
            # meant a held position that dipped out of eligibility for a
            # reason unrelated to price (say, a holder-count blip) silently
            # lost its strategy-exit protection until it requalified -- for
            # exactly the tokens most likely to need it. The file's own
            # comment above ("exits are never regime-gated") already states
            # the right principle; this just applies it consistently. A
            # house-money (profit_taken) position is excluded here since it
            # already has its own house-money trailing-stop path above and
            # deliberately has no other exit rule.
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
            sold_this_cycle.add(mint)

    # ---- scale in confirmed scout positions ----
    # "adding funds only if volume and price action confirm momentum": once a
    # scout position is up scale_in_price_threshold_pct from its (blended)
    # entry, top it up from the initial scout_position_fraction stake to the
    # full tier-target size, subject to the same risk caps (cash, aggregate
    # memecoin exposure, portfolio heat) as any new entry. Volume
    # confirmation is a documented simplification, not a silent gap: this
    # pipeline has no reliable live volume signal to check once a position is
    # already open (Jupiter's search endpoint used for pricing doesn't return
    # it), so price action alone is the real trigger here. Gated on
    # allow_new_entries like any other addition of fresh capital at risk --
    # the regime filter existing to block *new* risk during risk-off applies
    # to growing a position's exposure too, not just opening a brand new one.
    # not_scaled_in mirrors not_traded's audit purpose for this loop -- a
    # real gap found by mining the journal: after 383 cycles and a position
    # (GIKO) that clearly reached scaled_in=True in the live state, the
    # journal showed ZERO scale_in actions ever recorded. Root cause: the
    # "too small to bother" branch below silently flipped scaled_in=True
    # without ever buying anything and without logging why -- indistinguishable
    # from a real scale-in without reading raw state. Every branch that skips
    # a scale-in now records a reason, same discipline as new-entry rejections.
    not_scaled_in: dict[str, str] = {}
    if allow_new_entries:
        for mint, pos in list(state["positions"].items()):
            if pos.get("tier") != "scout" or pos.get("scaled_in") or pos.get("profit_taken"):
                continue
            price = prices.get(mint)
            if price is None:
                continue
            if price < pos["entry_price_usd"] * (1 + args.scale_in_price_threshold_pct):
                continue  # hasn't confirmed momentum yet -- not a rejection worth logging, just not due yet
            closes = get_price_history_closes(mint, args.history_days)
            full_target_size = size_position("scout", port_value, closes, args)
            cost_basis = pos.get("cost_basis_usd", 0.0)
            exposure_room = args.max_memecoin_exposure_fraction * port_value - memecoin_exposure_usd(state, prices)
            additional_needed, skip_reason = compute_scale_in_topup(
                cost_basis, full_target_size, state["cash_usd"], exposure_room, args.min_trade_usd)
            if skip_reason == "already_full":
                pos["scaled_in"] = True  # already close enough to full size, or too small a top-up to bother
                not_scaled_in[mint] = (f"full target size (${full_target_size:.2f}) is within min_trade_usd "
                                        f"(${args.min_trade_usd:.2f}) of what's already invested "
                                        f"(${cost_basis:.2f}) -- treating as fully sized, "
                                        f"marked scaled_in without buying more")
                continue
            if skip_reason == "insufficient_room":
                # Wanted a top-up but nothing (or too little) fits under
                # cash/exposure room right now -- retry next cycle, don't
                # mark scaled_in. Exposure has repeatedly sat near the cap
                # live (37 new-entry rejections for this exact reason), so
                # this is the common case, not an edge case.
                not_scaled_in[mint] = (f"wanted to add ${full_target_size - cost_basis:.2f} but insufficient "
                                        f"cash/exposure room fits under min_trade_usd (${args.min_trade_usd:.2f}) "
                                        f"-- retrying next cycle, not marked scaled_in")
                continue
            projected_heat = (portfolio_heat_pct(state, prices, args.stop_loss_pct)
                               + (additional_needed * args.stop_loss_pct / port_value if port_value else 0))
            if projected_heat > args.max_portfolio_heat_pct:
                not_scaled_in[mint] = f"would exceed max_portfolio_heat_pct ({projected_heat:.1%})"
                continue
            fill_price = price * (1 + args.slippage_bps / 10_000)
            added_quantity = (additional_needed * (1 - args.fee_bps / 10_000)) / fill_price
            state["cash_usd"] -= additional_needed
            total_quantity = pos["quantity"] + added_quantity
            total_cost = pos.get("cost_basis_usd", 0.0) + additional_needed
            pos["entry_price_usd"] = total_cost / total_quantity  # blended avg cost -- ret/stop-loss/take-profit all key off this
            pos["quantity"] = total_quantity
            pos["cost_basis_usd"] = total_cost
            # Only mark fully scaled-in if this top-up actually reached the
            # full target -- a capped partial top-up should get another
            # chance to top up the rest next cycle if room opens up.
            pos["scaled_in"] = total_cost >= full_target_size - 1e-9
            actions.append({"type": "scale_in", "symbol": pos["symbol"], "mint": mint,
                             "size_usd": additional_needed, "fill_price": fill_price,
                             "partial": not pos["scaled_in"],
                             "reason": f"price confirmed momentum (+{args.scale_in_price_threshold_pct:.0%} from scout entry)"})

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
        scout_open = sum(1 for p in state["positions"].values() if p.get("tier") == "scout")
        # Discovery now rotates through a ~2,600-token pool (see
        # select_candidates_for_rotation()), so most eligible candidates each
        # cycle are ones never seen before -- a price_history_cache miss,
        # meaning a fresh CoinGecko fetch. Observed live: cycles with 6-8
        # such candidates took 1m40s-1m50s (vs. ~13s typical) from repeated
        # 429 backoff. Capping fresh fetches per cycle bounds cycle duration;
        # candidates deferred this way get reconsidered next cycle (discovery
        # re-evaluates them fresh each time, nothing is lost, just delayed).
        fresh_fetches_this_cycle = 0
        for mint, result in eligible.items():
            if mint in state["positions"]:
                continue  # already held -- not a "skip", just not a new entry decision
            if mint in sold_this_cycle:
                # Mirrors config/risk.yaml's min_hours_between_trades_same_token
                # (4h), which the live trade-cycle skill enforces -- without
                # this, a take-profit/stop-loss exit and a same-cycle re-entry
                # at essentially the same price (nothing moved between the
                # sell and this check) would silently cancel out the exit's
                # purpose. Real trade: CATE hit take-profit and was bought
                # right back in the same cycle before this guard existed.
                not_traded[mint] = "sold this same cycle -- not re-entering immediately (mirrors live's min_hours_between_trades_same_token)"
                continue
            if recently_stopped_out(state["closed_trades"], mint, cycle_start, args.min_hours_between_trades_same_token):
                not_traded[mint] = (f"stopped out within the last {args.min_hours_between_trades_same_token}h -- "
                                     f"cooldown before re-entering the same token (mirrors live's "
                                     f"min_hours_between_trades_same_token)")
                continue
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
            if tier == "scout" and scout_open >= args.max_scout_positions:
                not_traded[mint] = f"scout-tier cap reached (max_scout_positions={args.max_scout_positions})"
                continue
            cached_closes = _load_price_history_cache(mint, args.history_days)
            if cached_closes is None and fresh_fetches_this_cycle >= MAX_FRESH_PRICE_HISTORY_FETCHES_PER_CYCLE:
                not_traded[mint] = (f"deferred to a future cycle (hit MAX_FRESH_PRICE_HISTORY_FETCHES_PER_CYCLE="
                                     f"{MAX_FRESH_PRICE_HISTORY_FETCHES_PER_CYCLE} -- bounds cycle duration when "
                                     f"discovery surfaces many never-before-seen candidates at once)")
                continue
            if cached_closes is None:
                fresh_fetches_this_cycle += 1
            closes = get_price_history_closes(mint, args.history_days, cached=cached_closes)
            time.sleep(args.request_delay)
            signal = compute_signal(closes) if closes else None
            if signal != "buy" and closes is not None:
                not_traded[mint] = f"strategy signal was '{signal}', not buy"
                continue  # only trade no-history tokens opportunistically-small; require an actual buy signal when history exists
            size_usd = size_position(tier, port_value, closes, args)
            is_scout = tier == "scout"
            if is_scout:
                # Initial scout entry starts at 20% of the tier-target size
                # -- "starting with a small initial scout position, adding
                # funds only if price action confirms momentum" (see the
                # scale-in loop above) -- then is floored UP to at least
                # --scout-min-trade-usd (default $2.50) if that's larger.
                # Explicit user request ("raise scouts to $2 or $3 per
                # token"): on this portfolio's size, the 20%-of-target
                # formula alone was landing at ~$1.35-1.55, well under what
                # was asked for. This is a floor, not a fixed size -- on a
                # larger portfolio the formula-driven amount can still
                # exceed it and grow normally from there. Raising this
                # doesn't raise the memecoin exposure ceiling
                # (max_memecoin_exposure_fraction, left at 20% per an
                # explicit decision not to increase it further) -- it just
                # means each scout slot costs more against that same fixed
                # ceiling, so fewer concurrent scout positions fit under it.
                size_usd = max(size_usd * args.scout_position_fraction, args.scout_min_trade_usd)
            elif size_usd < args.min_trade_usd:
                not_traded[mint] = f"sized position ${size_usd:.2f} below min_trade_usd (${args.min_trade_usd:.2f})"
                continue
            if size_usd > state["cash_usd"]:
                not_traded[mint] = f"sized position ${size_usd:.2f} exceeds available cash (${state['cash_usd']:.2f})"
                continue
            if tier in MEMECOIN_TIERS:
                # "Risk a maximum of X% of total portfolio on memecoins" --
                # aggregate cap across scout + emerging tiers combined,
                # independent of (and in addition to) the per-tier position
                # COUNT caps above, which don't bound total dollar exposure
                # on their own.
                current_exposure = memecoin_exposure_usd(state, prices)
                if current_exposure + size_usd > args.max_memecoin_exposure_fraction * port_value:
                    not_traded[mint] = (f"would exceed max_memecoin_exposure_fraction "
                                         f"(${current_exposure + size_usd:.2f} > "
                                         f"{args.max_memecoin_exposure_fraction:.0%} of ${port_value:.2f} portfolio)")
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
                "cost_basis_usd": size_usd, "scaled_in": not is_scout, "profit_taken": False,
            }
            actions.append({"type": "buy", "symbol": result["symbol"], "mint": mint, "tier": tier,
                             "size_usd": size_usd, "fill_price": fill_price,
                             "signal_basis": "buy signal" if closes else "no-history minimum-size entry"})
            open_slots -= 1
            if tier == "emerging":
                emerging_open += 1
            elif tier == "scout":
                scout_open += 1

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
        "not_scaled_in": {mint: {"symbol": state["positions"][mint]["symbol"], "reason": reason}
                           for mint, reason in not_scaled_in.items()},
        "open_positions": len(state["positions"]),
    })

    print(f"\nActions this cycle: {len(actions)}")
    for a in actions:
        if a["type"] == "buy":
            print(f"  BUY       {a['symbol']:>10}  ${a['size_usd']:.2f}  tier={a['tier']:<10}  ({a['signal_basis']})")
        elif a["type"] == "scale_in":
            partial_note = " [partial]" if a.get("partial") else ""
            print(f"  SCALE-IN  {a['symbol']:>10}  +${a['size_usd']:.2f}{partial_note}  ({a['reason']})")
        elif a["type"] == "partial_sell":
            print(f"  TAKE-PART {a['symbol']:>10}  sold {a['quantity_sold']:.4g} (${a['proceeds_usd']:.2f})  "
                  f"{a['reason']}, {a['remaining_quantity']:.4g} riding free")
        else:
            print(f"  SELL      {a['symbol']:>10}  {a['reason']}  return={a['return_pct']:+.1f}%")
    if not_traded:
        print(f"\nEligible but not traded ({len(not_traded)}):")
        for mint, reason in not_traded.items():
            print(f"  {eligible[mint]['symbol']:>10}  {reason}")
    if not_scaled_in:
        print(f"\nConfirmed momentum but not scaled in ({len(not_scaled_in)}):")
        for mint, reason in not_scaled_in.items():
            print(f"  {state['positions'][mint]['symbol']:>10}  {reason}")
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


def scout_disco_args(args) -> argparse.Namespace:
    """A second, deliberately looser threshold set for very-new/low-liquidity
    tokens the normal thresholds structurally can't pass -- min_pool_age_hours
    (72h) and min_liquidity_usd ($250k) both assume an already-established
    token, so a genuinely new one fails on age/liquidity alone regardless of
    fundamentals. PAPER-TRADING EXPERIMENTAL ONLY: this does not touch
    config/discovery.yaml or research/discover_candidates.py's own CLI
    defaults, which stay the real safety boundary for anything live (see
    CLAUDE.md rule 7) -- this is a controlled way to build a real paper track
    record on "scout" tier before ever considering loosening anything live.

    The hard anti-rug checks are UNCHANGED from disco_args(): mint/freeze
    authority must still be renounced and RugCheck must still confirm the
    mint isn't a known rug -- those are what actually prevent "the dev drains
    the pool," not age or liquidity depth. Only the maturity/liquidity/
    distribution dimensions are loosened, and only somewhat -- a brand-new
    token concentrated in a few wallets is normal for its first hours, not
    itself proof of malice, but max_top_holder_pct is still capped well
    below "one wallet owns most of the supply.\""""
    base = disco_args(args)
    base.min_liquidity_usd = args.scout_min_liquidity_usd
    base.min_holder_count = args.scout_min_holder_count
    base.min_organic_score = args.scout_min_organic_score
    base.min_pool_age_hours = args.scout_min_pool_age_hours
    base.max_top_holder_pct = args.scout_max_top_holder_pct
    return base


def memecoin_exposure_usd(state: dict, prices: dict[str, float]) -> float:
    """Mark-to-market value of every open position in MEMECOIN_TIERS (scout +
    emerging) -- what max_memecoin_exposure_fraction actually caps."""
    total = 0.0
    for mint, pos in state["positions"].items():
        if pos.get("tier") in MEMECOIN_TIERS:
            price = prices.get(mint)
            if price is not None:
                total += pos["quantity"] * price
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="Wipe paper state and restart at --starting-capital-usd")
    ap.add_argument("--starting-capital-usd", type=float, default=50.0)
    ap.add_argument("--max-candidates", type=int, default=250,
                     help="Was 40 -- raised to match config/discovery.yaml's max_candidates_per_cycle after the "
                          "live pathway (research/discover_candidates.py) needed it to actually surface rare real "
                          "candidates; paper trading's own rotation history (select_candidates_for_rotation) "
                          "already covered the pool well at 40 thanks to its faster cadence, but there's no "
                          "reason for the two to drift apart.")
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
    ap.add_argument("--max-concurrent-positions", dest="max_concurrent_positions", type=int, default=15,
                     help="Was 3, then 6, then 10, now 15 (explicit user request, to surface more of the "
                          "~2,600-token rotated discovery pool as actual positions instead of 'no open slots' "
                          "rejections). At 10, this had become the dominant rejection reason for most cycles in "
                          "a row -- discovery was routinely finding 10-16 eligible candidates/cycle with all 10 "
                          "slots full, so genuinely new, never-before-seen tokens were being turned away purely "
                          "on count, not on any risk check. Raising this doesn't raise dollar risk on its own -- "
                          "total memecoin exposure is still independently bounded by "
                          "max_memecoin_exposure_fraction, and non-memecoin tiers are still bounded by cash and "
                          "max_portfolio_heat_pct -- it only lets that same bounded exposure spread across more, "
                          "smaller positions instead of artificially throttling diversification.")
    ap.add_argument("--max-emerging-tier-positions", dest="max_emerging_tier_positions", type=int, default=2)
    ap.add_argument("--target-daily-volatility-pct", dest="target_daily_volatility_pct", type=float, default=3.0)
    ap.add_argument("--volatility-size-min-mult", dest="volatility_size_min_mult", type=float, default=0.5)
    ap.add_argument("--volatility-size-max-mult", dest="volatility_size_max_mult", type=float, default=1.5)
    ap.add_argument("--max-portfolio-heat-pct", dest="max_portfolio_heat_pct", type=float, default=0.12)
    ap.add_argument("--stop-loss-pct", dest="stop_loss_pct", type=float, default=0.15)
    ap.add_argument("--min-hours-between-trades-same-token", dest="min_hours_between_trades_same_token",
                     type=float, default=4.0,
                     help="Matches config/risk.yaml's value of the same name -- a stop-loss exit on a mint blocks "
                          "re-entering that same mint until this many hours pass (see recently_stopped_out()). "
                          "Added after mining the journal found CEZ stopped out, was re-bought ~2h later (running "
                          "two independent loops against the same state meant cadence between cycles was faster "
                          "than either loop's own interval), and immediately stopped out again.")
    ap.add_argument("--take-profit-pct", dest="take_profit_pct", type=float, default=0.35)
    ap.add_argument("--trailing-stop-pct", dest="trailing_stop_pct", type=float, default=0.12)
    ap.add_argument("--scout-trailing-stop-pct", dest="scout_trailing_stop_pct", type=float, default=0.08,
                     help="Tighter than --trailing-stop-pct, applies to scout tier only (pre-profit-take). "
                          "Explicit user request: 'chase highs up and sell as soon as they go down' for these "
                          "higher-risk, low-dollar positions -- react faster than the 12%% other tiers use, since "
                          "the whole point of scout is exploiting fast moves on thin/new tokens, not riding out "
                          "their full volatility. Once profit_taken fires, house_money_trailing_stop_pct (wider) "
                          "takes over instead, so a proven winner still gets room to run.")
    ap.add_argument("--house-money-trailing-stop-pct", dest="house_money_trailing_stop_pct", type=float, default=0.30,
                     help="Wider than --trailing-stop-pct -- applies only after a scout position's profit-take "
                          "fires (cost basis already recouped, nothing left to lose), replacing the normal "
                          "stop-loss/take-profit entirely so 'let it ride for upside potential' actually gets "
                          "room to run instead of being chopped by routine volatility.")
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
    # ---- Scout tier: tiered entry into very-new/low-liquidity tokens ------------
    # PAPER-TRADING EXPERIMENTAL. Does not touch config/discovery.yaml or
    # config/risk.yaml -- see scout_disco_args()'s docstring. "Scout" tokens
    # fail the normal min-pool-age/min-liquidity thresholds (which assume an
    # already-established token) but still pass every hard anti-rug check
    # (mint/freeze authority renounced, RugCheck not-rugged). Sized tiny
    # (scout-position-fraction of the tier target), scaled up only if price
    # confirms momentum, and capped in aggregate by max-memecoin-exposure-fraction.
    ap.add_argument("--enable-scout-tier", dest="enable_scout_tier", action="store_true", default=True)
    ap.add_argument("--disable-scout-tier", dest="enable_scout_tier", action="store_false")
    ap.add_argument("--scout-min-liquidity-usd", dest="scout_min_liquidity_usd", type=float, default=20_000,
                     help="vs. min_liquidity_usd's $250k -- a real new pool can be legitimate with far less depth")
    ap.add_argument("--scout-min-holder-count", dest="scout_min_holder_count", type=int, default=30,
                     help="vs. min_holder_count's 500 -- a token a few hours old hasn't had time to accumulate holders")
    ap.add_argument("--scout-min-organic-score", dest="scout_min_organic_score", type=float, default=20,
                     help="vs. min_organic_score's 40 -- still required (missing organicScore still hard-rejects, "
                          "same wash-trading defense as the normal tier), just a lower bar")
    ap.add_argument("--scout-min-pool-age-hours", dest="scout_min_pool_age_hours", type=float, default=1,
                     help="vs. min_pool_age_hours's 72 -- still excludes the first hour (the single highest-risk "
                          "rug window), the whole point of this tier is reaching tokens that are genuinely new")
    ap.add_argument("--scout-max-top-holder-pct", dest="scout_max_top_holder_pct", type=float, default=35.0,
                     help="vs. max_top_holder_pct's 22%% -- early concentration is normal for a token's first "
                          "hours, not on its own proof of malice, but still capped well below 'one wallet owns "
                          "most of the supply'")
    ap.add_argument("--scout-position-fraction", dest="scout_position_fraction", type=float, default=0.20,
                     help="initial scout entry = this fraction of the tier-target size ('starting with a small "
                          "initial scout position')")
    ap.add_argument("--scout-min-trade-usd", dest="scout_min_trade_usd", type=float, default=2.5,
                     help="floor an initial scout entry up to at least this much (see the new-entry loop's "
                          "is_scout branch) rather than rejecting it outright like --min-trade-usd does for "
                          "other tiers. Was $1.00 (the formula-driven 20%%-of-target size alone landed at "
                          "~$1.35-1.55 on this portfolio); raised to $2.50 on explicit user request ('raise "
                          "scouts to $2 or $3 per token'). Still just a floor -- a larger portfolio's "
                          "formula-driven size can exceed it and grow normally from there.")
    ap.add_argument("--scale-in-price-threshold-pct", dest="scale_in_price_threshold_pct", type=float, default=0.15,
                     help="price up this much from scout entry = 'price action confirms momentum' -> top up to "
                          "the full tier-target size")
    ap.add_argument("--profit-take-multiple", dest="profit_take_multiple", type=float, default=2.0,
                     help="sell enough to recoup 100%% of cost basis once position value reaches this multiple of "
                          "cost basis ('2x to 3x gain') -- the remainder rides with zero capital still at risk. "
                          "Was 2.5x; lowered to 2.0x (the low end of that original range, not below it) per "
                          "explicit user request to 'take profits sooner so slots free up faster' -- this also "
                          "directly frees max_memecoin_exposure_fraction room sooner, since "
                          "memecoin_exposure_usd() marks to market (quantity * price), and a profit-take reduces "
                          "quantity, not just cost basis.")
    ap.add_argument("--max-scout-positions", dest="max_scout_positions", type=int, default=8,
                     help="separate count cap from --max-emerging-tier-positions -- scout sizes are much smaller "
                          "individually, so more concurrent slots still keeps aggregate exposure bounded by "
                          "--max-memecoin-exposure-fraction. Was 5, raised after it became the binding constraint "
                          "for 2 cycles running (candidates rejected 'scout-tier cap reached') while dollar "
                          "exposure had room to spare (verified live: $7.05 of a $12.09 budget) -- same reasoning "
                          "as --max-concurrent-positions's earlier increase.")
    ap.add_argument("--max-memecoin-exposure-fraction", dest="max_memecoin_exposure_fraction", type=float, default=0.20,
                     help="aggregate cap on scout+emerging tier value as a fraction of total portfolio value. "
                          "Was 0.05 ('risk a maximum of 5%% of total portfolio on memecoins', the original ask) "
                          "-- raised to 0.20 by explicit request after a single pre-existing emerging position "
                          "(BULLSHIT, $6.45, ~11.5%% of portfolio, bought before this cap existed) alone exceeded "
                          "the 5%% budget and blocked every scout/emerging entry indefinitely. Independent of the "
                          "per-tier position COUNT caps, which don't bound total dollar exposure on their own.")
    args = ap.parse_args()
    with CycleLock(LOCK_PATH):
        run_cycle(args)


if __name__ == "__main__":
    main()
