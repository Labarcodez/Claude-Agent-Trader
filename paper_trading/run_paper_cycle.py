#!/usr/bin/env python3
"""Paper-trading simulator: runs the SAME discovery + strategy + risk logic
the live `trade-cycle` skill uses, against real live Kraken market data, but
never places a real order. Fills are simulated at current market price with
the same fee/slippage assumptions as backtest/engine.py. State lives in
state/paper_portfolio.json; every cycle appends to journal/paper_trades.jsonl
-- kept completely separate from journal/trades.jsonl (real trading history).

Why this exists: it validates the full autonomous pipeline end-to-end --
including on pairs research/discover_candidates.py finds fresh, which have
no track record of their own -- before any real money is at risk. It's
runnable from a session with no Kraken API key at all, since it only reads
public market data (Kraken's AssetPairs/Ticker/OHLC, CoinGecko for market
caps). Run it repeatedly (e.g. via /loop) to build a track record while
deciding whether to trust the system live; see docs/RUNBOOK.md "Paper
trading before going live".

Simplifications vs. the live trade-cycle skill (documented, not hidden):
  - No real order validation, so no live slippage/spread check beyond what
    discovery already screened for -- a fixed fee_bps + slippage_bps cost is
    assumed instead (same convention as backtest/engine.py).
  - No max_daily_trade_count / max_daily_volume_usd cadence caps -- this
    script is typically run manually or via /loop at a deliberate interval,
    so cadence is controlled by how often you run it. Two same-pair re-entry
    guards DO exist: a pair sold this cycle can't be bought back in the SAME
    cycle, and a pair stopped out via stop-loss can't be re-bought for
    --min-hours-between-trades-same-token (default 4h, matches
    config/risk.yaml) -- see recently_stopped_out().
  - Core position sizing, tiering, volatility scaling, portfolio heat,
    regime filter, and stop-loss/take-profit/trailing-stop ARE all real,
    reusing the exact same code (backtest/strategies.py,
    research/discover_candidates.py) the live skill is documented to use.

Unlike the old Solana pipeline, there's no discovery-rotation-across-cycles
concept here: Kraken's full USD-pair universe (a few hundred pairs) is cheap
enough to evaluate in full every cycle (two bulk public API calls), so
nothing needs to be sampled or rotated in from a much larger pool the way
Jupiter's ~2,600-token pool required.

Scout tier (PAPER-TRADING EXPERIMENTAL, see scout_disco_args()): a second,
looser discovery pass for pairs too illiquid to pass the normal thresholds,
entered small (--scout-position-fraction, default 20% of the tier-target
size) and scaled to full size only if price rises
--scale-in-price-threshold-pct from entry ("price action confirms
momentum"). Sells enough to recoup 100% of cost basis once value reaches
--profit-take-multiple (default 2.0x), letting the remainder ride with zero
capital still at risk, on a tighter --scout-trailing-stop-pct (default 8%)
than other tiers get pre-profit-take. Capped in aggregate by
--max-memecoin-exposure-fraction (default 20% of portfolio, scout+emerging
combined). Does NOT touch config/discovery.yaml or config/risk.yaml -- those
stay the real safety boundary for anything live (CLAUDE.md rule 7); this is
how a strategy like this gets a real paper track record before that
conversation ever happens. Disable with --disable-scout-tier.

Usage:
    python3 paper_trading/run_paper_cycle.py
    python3 paper_trading/run_paper_cycle.py --reset       # wipe paper state, restart at starting capital
    python3 paper_trading/run_paper_cycle.py --max-candidates 10
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from research import discover_candidates as disco  # noqa: E402
from backtest import fetch_history as fh  # noqa: E402
from backtest import strategies as strat  # noqa: E402
from kraken import client as kc  # noqa: E402
from kraken import fees as kf  # noqa: E402
from scripts.cycle_lock import CycleLock  # noqa: E402

STATE_PATH = REPO_ROOT / "state" / "paper_portfolio.json"
JOURNAL_PATH = REPO_ROOT / "journal" / "paper_trades.jsonl"
PAPER_DISCOVERY_HISTORY_PATH = REPO_ROOT / "state" / "paper_discovery_history.json"
LOCK_PATH = REPO_ROOT / "state" / "paper_cycle.lock"
REGIME_CACHE_PATH = REPO_ROOT / "state" / "regime_cache.json"
REGIME_CACHE_TTL_SECONDS = 3600  # regime is a daily-scale (30d SMA) signal -- refetching every 15min cycle is
                                  # unnecessary load for no real freshness gain
PRICE_HISTORY_CACHE_DIR = REPO_ROOT / "state" / "price_history_cache"
PRICE_HISTORY_CACHE_TTL_SECONDS = 3600  # ceiling, used as-is for the daily default (1440min bars) -- a daily
                                          # close doesn't change intra-day at all, so checking hourly is already
                                          # conservative. _price_history_cache_ttl_seconds() below scales this
                                          # down for shorter --interval-minutes bars so a fresh 15m/5m candle is
                                          # actually picked up within roughly one bar's width, not served stale
                                          # for up to an hour regardless of granularity (a real bug for
                                          # day-trading mode until 2026-08-26: this constant predates
                                          # --interval-minutes and was never revisited when that was added).
MAX_FRESH_PRICE_HISTORY_FETCHES_PER_CYCLE = 250  # bounds cycle duration when discovery surfaces many
                                                  # never-before-cached pairs at once -- a safety ceiling, not an
                                                  # active target, same convention as discover_candidates.py's
                                                  # --max-candidates=700 vs Kraken's real ~627-pair universe.
                                                  # Raised 2026-08-26, twice: 30 -> 80 -> 250. Kraken's eligible
                                                  # universe currently runs ~150/cycle; 30 meant 5+ cycles just to
                                                  # get a first look at every eligible pair, and even 80 needed 2
                                                  # -- both easy to misread as "nothing is buyable" when it's
                                                  # really "most pairs haven't been checked yet" (see
                                                  # docs/STRATEGY.md "Trade frequency, not just trade quality").
                                                  # 250 clears today's full eligible set in ONE cycle from cold
                                                  # (verified live 2026-08-26: 148/149 eligible pairs checked,
                                                  # 0 deferred, a real buy signal surfaced) with headroom for the
                                                  # eligible count to grow before this needs revisiting again.
                                                  # Kraken's OHLC endpoint is a single fast, reliable upstream
                                                  # (unlike the old CoinGecko-by-contract path, which stacked
                                                  # retries against a much slower, more rate-limited API) --
                                                  # deferred candidates are simply reconsidered next cycle, no
                                                  # correctness loss.

TIER_MULTIPLIERS = {"blue_chip": 1.0, "established": 0.7, "emerging": 0.4, "scout": 0.4}
MEMECOIN_TIERS = {"scout", "emerging"}  # what counts toward --max-memecoin-exposure-fraction. Naming predates
                                          # the Kraken migration and config/risk.yaml no longer has a field by
                                          # this name -- on Kraken this means "low-cap/low-volume", not literally
                                          # memecoins, and this whole cap is a paper-trading-only concept (like
                                          # the "scout" tier itself), not yet formalized in risk.yaml/trade-cycle.


# ---- State ------------------------------------------------------------------

def load_state(starting_capital_usd: float, reset: bool) -> dict:
    if STATE_PATH.exists() and not reset:
        return json.loads(STATE_PATH.read_text())
    state = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "starting_capital_usd": starting_capital_usd,
        "cash_usd": starting_capital_usd,
        "positions": {},   # pair -> {symbol, quantity, entry_price_usd, entry_time, tier, peak_price_usd}
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

def current_prices(pairs: list[str]) -> dict[str, float]:
    """Live mid-price (ask+bid)/2 in USD for many Kraken pairs in as few
    Ticker calls as possible -- kraken/client.py's ticker() already batches
    in CHUNK_SIZE-sized groups. A pair Kraken's Ticker doesn't return (rare)
    is simply absent from the result, same contract as the old Jupiter-based
    version."""
    prices: dict[str, float] = {}
    if not pairs:
        return prices
    try:
        raw = kc.ticker(pairs)
    except kc.KrakenAPIError:
        return prices
    for name, t in raw.items():
        try:
            ask, bid = float(t["a"][0]), float(t["b"][0])
            mid = (ask + bid) / 2 if (ask and bid) else float(t["c"][0])
            if mid > 0:
                prices[name] = mid
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    return prices


def portfolio_value_usd(state: dict, prices: dict[str, float]) -> float:
    total = state["cash_usd"]
    for pair, pos in state["positions"].items():
        price = prices.get(pair)
        if price is not None:
            total += pos["quantity"] * price
    return total


def all_positions_priced(state: dict, prices: dict[str, float]) -> bool:
    """False if any open position is missing a price this cycle. See the
    original Solana-pipeline version of this function for the full
    reasoning (a real circuit-breaker false-positive this guards against) --
    unchanged here, just pair-keyed instead of mint-keyed."""
    return all(pair in prices for pair in state["positions"])


# ---- Regime filter ------------------------------------------------------------

def _load_regime_cache(reference_pair: str, sma_window_days: int) -> bool | None:
    if not REGIME_CACHE_PATH.exists():
        return None
    try:
        cached = json.loads(REGIME_CACHE_PATH.read_text())
        if cached.get("reference_pair") != reference_pair or cached.get("sma_window_days") != sma_window_days:
            return None
        computed_at = datetime.fromisoformat(cached["computed_at"])
        age_seconds = (datetime.now(timezone.utc) - computed_at).total_seconds()
        if age_seconds > REGIME_CACHE_TTL_SECONDS:
            return None
        return cached["allows_new_entries"]
    except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
        return None


def _save_regime_cache(reference_pair: str, sma_window_days: int, allows_new_entries: bool) -> None:
    REGIME_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGIME_CACHE_PATH.write_text(json.dumps({
        "reference_pair": reference_pair,
        "sma_window_days": sma_window_days,
        "allows_new_entries": allows_new_entries,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }))


def regime_allows_new_entries(reference_pair: str, sma_window_days: int) -> bool:
    """A 30-day SMA doesn't meaningfully change within a 15-minute paper-cycle
    interval, so this is cached for REGIME_CACHE_TTL_SECONDS instead of hit
    live every cycle. Sourced from Kraken's own OHLC endpoint (no CoinGecko
    dependency, no API key)."""
    cached = _load_regime_cache(reference_pair, sma_window_days)
    if cached is not None:
        return cached
    try:
        payload = fh.fetch_ohlc_kraken(reference_pair, days=sma_window_days + 5)
    except Exception as e:
        print(f"  ! regime filter data unavailable ({e}) -- defaulting to conservative (no new entries)", file=sys.stderr)
        return False
    closes = [p for _, p in payload.get("prices", [])]
    if len(closes) < sma_window_days:
        return False
    sma = sum(closes[-sma_window_days:]) / sma_window_days
    result = closes[-1] > sma
    _save_regime_cache(reference_pair, sma_window_days, result)
    return result


# ---- Signal generation --------------------------------------------------------

def _price_history_cache_path(pair: str, days: int, interval_minutes: int = kc.OHLC_DAILY_INTERVAL_MINUTES) -> Path:
    # Daily (the original, still-default granularity) keeps the original
    # unsuffixed filename -- same backward-compat convention as
    # backtest/fetch_history.py's cache_key_for_kraken().
    suffix = "" if interval_minutes == kc.OHLC_DAILY_INTERVAL_MINUTES else f"_{interval_minutes}m"
    return PRICE_HISTORY_CACHE_DIR / f"{pair}{suffix}_{days}d.json"


def _price_history_cache_ttl_seconds(interval_minutes: int) -> int:
    """A new bar exists roughly every interval_minutes -- serving a cached
    signal for longer than that risks missing the exact bar a strategy like
    ema_ribbon/donchian_channel_breakout cares about (they fire on the bar
    a condition first becomes newly true, not on every bar it holds).
    Capped at PRICE_HISTORY_CACHE_TTL_SECONDS, which only actually binds
    for the daily default (1440min) -- for every shorter --interval-minutes
    this scales the TTL down to match the bar width instead."""
    return min(PRICE_HISTORY_CACHE_TTL_SECONDS, interval_minutes * 60)


def _load_price_history_cache(pair: str, days: int, interval_minutes: int = kc.OHLC_DAILY_INTERVAL_MINUTES) -> list[float] | None:
    path = _price_history_cache_path(pair, days, interval_minutes)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text())
        computed_at = datetime.fromisoformat(cached["computed_at"])
        if (datetime.now(timezone.utc) - computed_at).total_seconds() > _price_history_cache_ttl_seconds(interval_minutes):
            return None
        return cached["closes"]
    except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
        return None


def _save_price_history_cache(pair: str, days: int, closes: list[float], interval_minutes: int = kc.OHLC_DAILY_INTERVAL_MINUTES) -> None:
    PRICE_HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _price_history_cache_path(pair, days, interval_minutes).write_text(json.dumps({
        "closes": closes,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }))


def get_price_history_closes(pair: str, days: int, cached: list[float] | None = None,
                              interval_minutes: int = kc.OHLC_DAILY_INTERVAL_MINUTES) -> list[float] | None:
    """Cached for _price_history_cache_ttl_seconds(interval_minutes) per
    (pair, days, interval_minutes). Pass `cached` if the caller already did its own
    _load_price_history_cache() lookup -- avoids re-reading the same cache
    file twice. Uses an impatient retry policy (2 attempts, 3s base wait)
    for the same reason the old pipeline did: this runs inside a tight cron
    loop where a candidate this gives up on quickly just gets reconsidered
    next cycle.

    interval_minutes defaults to daily (unchanged behavior for every
    existing caller) -- pass e.g. 60 for the validated day-trading
    timeframe (see docs/STRATEGY.md "Day trading -- what the evidence
    actually supports")."""
    if cached is None:
        cached = _load_price_history_cache(pair, days, interval_minutes)
    if cached is not None:
        return cached
    try:
        payload = fh.fetch_ohlc_kraken(pair, days=days, interval_minutes=interval_minutes, retries=2, backoff=1.5)
    except Exception:
        return None
    prices = payload.get("prices") or []
    if len(prices) < 15:
        return None
    closes = [p for _, p in prices]
    _save_price_history_cache(pair, days, closes, interval_minutes)
    return closes


def compute_signal(closes: list[float], strategy_name: str = "adaptive_ensemble") -> str:
    """strategy_name defaults to the live default (adaptive_ensemble) --
    override via --strategy to paper-track a candidate strategy (e.g.
    macd_crossover) against real closed round trips instead of trusting a
    backtest's mark-to-market snapshot alone (see docs/STRATEGY.md's
    caution on macd_crossover's backtest-only numbers)."""
    return strat.STRATEGIES[strategy_name](closes, len(closes) - 1, {})


def realized_vol_pct(closes: list[float]) -> float | None:
    vol = strat.realized_vol(closes, window=min(20, max(2, len(closes) - 1)))
    return vol * 100 if vol is not None else None


def real_taker_fee_bps_if_available(default_bps: float) -> float:
    """Tries the account's REAL current Kraken fee tier
    (kraken.client.trade_volume(), via kraken/fees.py's parse_fee_tier())
    once per cycle, for the fee-aware edge-cost gate below to check against
    -- falls back to `default_bps` (the flat --fee-bps assumption) if no
    API key is configured, or the call fails for any reason. Kraken's fee
    tier is account-wide (based on 30-day volume), not meaningfully
    different pair-to-pair for ordinary spot pairs, so one lookup (using
    BTC/USD as a representative pair) is reused for every candidate this
    cycle rather than one call per candidate."""
    if not (os.environ.get("KRAKEN_API_KEY") and os.environ.get("KRAKEN_API_SECRET")):
        return default_bps
    try:
        tier = kf.parse_fee_tier(kc.trade_volume("XBTUSD"), "XBTUSD", default_bps, default_bps)
        return tier.taker_fee_bps
    except kc.KrakenAPIError:
        return default_bps


# ---- Sizing & risk --------------------------------------------------------------

def size_position(tier: str, portfolio_value: float, closes: list[float] | None, args) -> float:
    base = min(args.max_position_usd, portfolio_value * args.max_position_fraction)
    base *= TIER_MULTIPLIERS.get(tier, 0.4)
    if closes is None:
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
    or None if not triggered. Unchanged from the original pipeline."""
    if cost_basis_usd <= 0:
        return None
    current_value = quantity * price
    if current_value < cost_basis_usd * profit_take_multiple:
        return None
    return min(cost_basis_usd / price, quantity)


def compute_scale_in_topup(cost_basis_usd: float, full_target_size: float, cash_usd: float,
                            exposure_room_usd: float, min_trade_usd: float) -> tuple[float, str | None]:
    """Pure sizing decision for a scout position's momentum-confirmed
    scale-in. Unchanged from the original pipeline."""
    additional_needed = full_target_size - cost_basis_usd
    if additional_needed < min_trade_usd:
        return 0.0, "already_full"
    capped_needed = min(additional_needed, cash_usd, max(exposure_room_usd, 0.0))
    if capped_needed < min_trade_usd:
        return 0.0, "insufficient_room"
    return capped_needed, None


def recently_stopped_out(closed_trades: list[dict], pair: str, now: datetime, cooldown_hours: float) -> bool:
    """True if `pair` was closed via a stop-loss within the last
    cooldown_hours -- mirrors config/risk.yaml's
    min_hours_between_trades_same_token. Unchanged from the original
    pipeline other than the mint->pair rename."""
    for trade in closed_trades:
        if trade.get("pair") != pair or "stop-loss" not in (trade.get("reason") or ""):
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
    for pair, pos in state["positions"].items():
        price = prices.get(pair)
        if price is not None:
            heat_usd += pos["quantity"] * price * stop_loss_pct
    return heat_usd / value


def memecoin_exposure_usd(state: dict, prices: dict[str, float]) -> float:
    """Mark-to-market value of every open position in MEMECOIN_TIERS (scout +
    emerging) -- what max_memecoin_exposure_fraction actually caps."""
    total = 0.0
    for pair, pos in state["positions"].items():
        if pos.get("tier") in MEMECOIN_TIERS:
            price = prices.get(pair)
            if price is not None:
                total += pos["quantity"] * price
    return total


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
    try:
        candidates = disco.gather_candidates(args)
    except kc.KrakenAPIError as e:
        # Degrade to "no new candidates this cycle," not a crash. Before
        # this guard, a Kraken outage/429 burst beyond gather_candidates()'s
        # own retry budget raised uncaught here, which meant the exit-
        # management loop below (stop-loss/take-profit/trailing-stop on
        # EXISTING positions) never ran for this cycle either -- a real
        # regression from "exits are never regime-gated," found by code
        # review. `candidates = {}` still lets pricing/exits proceed
        # normally below (they key off state["positions"], not discovery);
        # only new entries are skipped this cycle (eligible ends up empty),
        # which is the correct fail-safe direction -- protecting existing
        # capital always outranks finding a new trade.
        print(f"  ! discovery unavailable this cycle ({e}) -- skipping new entries, "
              f"still managing existing positions", file=sys.stderr)
        candidates = {}
    held_pairs = set(state["positions"])
    all_pairs = sorted(candidates, key=lambda p: candidates[p]["volume_24h_usd"], reverse=True)
    # Held positions are always considered regardless of the volume-rank cap
    # -- an existing position must never silently fall out of consideration
    # for a strategy-driven exit just because it ranks below max_candidates
    # this cycle (config/discovery.yaml's pinned_candidates documents this
    # same intent).
    pairs = list(dict.fromkeys([p for p in all_pairs if p in held_pairs] + all_pairs))[: max(args.max_candidates, len(held_pairs))]

    market_caps = disco.market_caps_for_pairs(candidates, pairs)

    eligible: dict[str, dict] = {}
    rejected_count = 0
    scout_count = 0
    discovery_history = disco.load_discovery_history(PAPER_DISCOVERY_HISTORY_PATH)
    for pair in pairs:
        data = candidates[pair]
        mcap_usd = market_caps.get(pair, 0)
        result = disco.evaluate_candidate(pair, data, disco_args(args), mcap_usd=mcap_usd)
        final_result = result
        # Failed the normal thresholds -- try the looser scout thresholds
        # before giving up. Only worth the extra evaluation for candidates
        # that didn't already qualify normally.
        if not result["eligible"] and args.enable_scout_tier:
            scout_result = disco.evaluate_candidate(pair, data, scout_disco_args(args), mcap_usd=mcap_usd)
            if scout_result["eligible"]:
                scout_result["tier"] = "scout"
                final_result = scout_result
        final_result["data"]["trend"] = disco.record_and_compute_trend(
            pair, final_result["symbol"], final_result["data"], discovery_history
        )
        if final_result["eligible"]:
            eligible[pair] = final_result
            if final_result.get("tier") == "scout":
                scout_count += 1
            continue
        rejected_count += 1
    disco.save_discovery_history(discovery_history, PAPER_DISCOVERY_HISTORY_PATH)
    print(f"Discovery: {len(candidates)} found, {len(pairs)} evaluated, {len(eligible)} eligible "
          f"({scout_count} scout-tier), {rejected_count} rejected.")

    # One fee-tier lookup for the whole cycle (not per-candidate) -- see
    # real_taker_fee_bps_if_available()'s docstring for why one call
    # suffices, and why it gracefully falls back to the flat --fee-bps
    # assumption rather than requiring a Kraken API key.
    effective_taker_fee_bps = real_taker_fee_bps_if_available(args.fee_bps)
    if effective_taker_fee_bps != args.fee_bps:
        print(f"Using real account fee tier for the edge-cost check: {effective_taker_fee_bps:.0f}bps "
              f"(assumed default was {args.fee_bps:.0f}bps)")

    tracked_pairs = set(eligible) | set(state["positions"])

    # gather_candidates() already computed a mid-price for every pair it
    # returned (from the same Ticker call discovery just made) -- reuse
    # that instead of immediately re-fetching Ticker for the same pairs
    # again. Real waste found by code review: every cycle was making a
    # full second round of chunked Ticker calls purely to recompute a
    # price already sitting in `candidates`, doubling Kraken API traffic
    # for no new information. Only pairs NOT already priced by discovery
    # (a held position that fell out of the tracked universe, e.g. a pair
    # that went offline) still need a fresh fetch.
    prices = {p: candidates[p]["price_usd"] for p in tracked_pairs if p in candidates}
    still_needed = [p for p in tracked_pairs if p not in prices]
    if still_needed:
        time.sleep(0.5)
        prices.update(current_prices(still_needed))

    unpriced_positions = [pair for pair in state["positions"] if pair not in prices]
    if unpriced_positions:
        prices.update(current_prices(unpriced_positions))

    port_value = portfolio_value_usd(state, prices)
    port_value_reliable = all_positions_priced(state, prices)

    if not port_value_reliable:
        unpriced = [pair for pair in state["positions"] if pair not in prices]
        print(f"  ! {len(unpriced)}/{len(state['positions'])} open position(s) still couldn't be priced this "
              f"cycle after a retry -- skipping circuit breaker check, peak update, AND this cycle's stop-loss/"
              f"take-profit check for the affected position(s) (no reliable price to check them against)", file=sys.stderr)
    else:
        if port_value > state["peak_portfolio_value_usd"]:
            state["peak_portfolio_value_usd"] = port_value

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
    sold_this_cycle: set[str] = set()

    # ---- manage existing positions first (exits are never regime-gated) ----
    for pair, pos in list(state["positions"].items()):
        price = prices.get(pair)
        if price is None:
            continue
        pos["peak_price_usd"] = max(pos.get("peak_price_usd", pos["entry_price_usd"]), price)

        if pos.get("tier") == "scout" and not pos.get("profit_taken"):
            sell_qty = compute_partial_profit_take(pos.get("cost_basis_usd", 0.0), pos["quantity"], price,
                                                     args.profit_take_multiple)
            if sell_qty is not None:
                proceeds = sell_qty * price * (1 - args.fee_bps / 10_000 - args.slippage_bps / 10_000)
                state["cash_usd"] += proceeds
                pos["quantity"] -= sell_qty
                pos["cost_basis_usd"] = 0.0
                pos["profit_taken"] = True
                actions.append({"type": "partial_sell", "symbol": pos["symbol"], "pair": pair,
                                 "reason": f"profit-take ({args.profit_take_multiple:.1f}x cost basis)",
                                 "quantity_sold": sell_qty, "proceeds_usd": proceeds,
                                 "remaining_quantity": pos["quantity"], "tier": pos.get("tier"), "strategy": args.strategy})
                continue

        ret = (price / pos["entry_price_usd"]) - 1
        drawdown_from_peak = (price / pos["peak_price_usd"]) - 1
        exit_reason = None
        if pos.get("profit_taken"):
            if drawdown_from_peak <= -args.house_money_trailing_stop_pct:
                exit_reason = f"house-money trailing-stop ({drawdown_from_peak:+.1%} from peak)"
        else:
            take_profit_threshold = None if pos.get("tier") == "scout" else args.take_profit_pct
            trailing_stop_threshold = args.scout_trailing_stop_pct if pos.get("tier") == "scout" else args.trailing_stop_pct
            if ret <= -args.stop_loss_pct:
                exit_reason = f"stop-loss ({ret:+.1%})"
            elif take_profit_threshold is not None and ret >= take_profit_threshold:
                exit_reason = f"take-profit ({ret:+.1%})"
            elif ret > 0 and drawdown_from_peak <= -trailing_stop_threshold:
                exit_reason = f"trailing-stop ({drawdown_from_peak:+.1%} from peak)"
        if exit_reason is None and not pos.get("profit_taken"):
            # Deliberately NOT gated on `pair in eligible` -- eligibility is
            # a discovery-time safety gate about whether it's safe to newly
            # BUY a pair, not about its price trend. See the original
            # pipeline's identical reasoning.
            closes = get_price_history_closes(pair, args.history_days, interval_minutes=args.interval_minutes)
            if closes and compute_signal(closes, args.strategy) == "sell":
                exit_reason = "strategy sell signal"
        if exit_reason:
            proceeds = pos["quantity"] * price * (1 - args.fee_bps / 10_000 - args.slippage_bps / 10_000)
            state["cash_usd"] += proceeds
            state["closed_trades"].append({
                "symbol": pos["symbol"], "pair": pair, "entry_price_usd": pos["entry_price_usd"],
                "exit_price_usd": price, "return_pct": ret * 100, "reason": exit_reason,
                "closed_at": cycle_start.isoformat(), "tier": pos.get("tier"), "strategy": args.strategy,
            })
            actions.append({"type": "sell", "symbol": pos["symbol"], "pair": pair, "reason": exit_reason,
                             "return_pct": ret * 100, "tier": pos.get("tier"), "strategy": args.strategy})
            del state["positions"][pair]
            sold_this_cycle.add(pair)

    # ---- scale in confirmed scout positions ----
    not_scaled_in: dict[str, str] = {}
    if allow_new_entries:
        for pair, pos in list(state["positions"].items()):
            if pos.get("tier") != "scout" or pos.get("scaled_in") or pos.get("profit_taken"):
                continue
            price = prices.get(pair)
            if price is None:
                continue
            if price < pos["entry_price_usd"] * (1 + args.scale_in_price_threshold_pct):
                continue
            closes = get_price_history_closes(pair, args.history_days, interval_minutes=args.interval_minutes)
            full_target_size = size_position("scout", port_value, closes, args)
            cost_basis = pos.get("cost_basis_usd", 0.0)
            exposure_room = args.max_memecoin_exposure_fraction * port_value - memecoin_exposure_usd(state, prices)
            additional_needed, skip_reason = compute_scale_in_topup(
                cost_basis, full_target_size, state["cash_usd"], exposure_room, args.min_trade_usd)
            if skip_reason == "already_full":
                pos["scaled_in"] = True
                not_scaled_in[pair] = (f"full target size (${full_target_size:.2f}) is within min_trade_usd "
                                        f"(${args.min_trade_usd:.2f}) of what's already invested "
                                        f"(${cost_basis:.2f}) -- treating as fully sized, "
                                        f"marked scaled_in without buying more")
                continue
            if skip_reason == "insufficient_room":
                not_scaled_in[pair] = (f"wanted to add ${full_target_size - cost_basis:.2f} but insufficient "
                                        f"cash/exposure room fits under min_trade_usd (${args.min_trade_usd:.2f}) "
                                        f"-- retrying next cycle, not marked scaled_in")
                continue
            projected_heat = (portfolio_heat_pct(state, prices, args.stop_loss_pct)
                               + (additional_needed * args.stop_loss_pct / port_value if port_value else 0))
            if projected_heat > args.max_portfolio_heat_pct:
                not_scaled_in[pair] = f"would exceed max_portfolio_heat_pct ({projected_heat:.1%})"
                continue
            fill_price = price * (1 + args.slippage_bps / 10_000)
            added_quantity = (additional_needed * (1 - args.fee_bps / 10_000)) / fill_price
            state["cash_usd"] -= additional_needed
            total_quantity = pos["quantity"] + added_quantity
            total_cost = pos.get("cost_basis_usd", 0.0) + additional_needed
            pos["entry_price_usd"] = total_cost / total_quantity
            pos["quantity"] = total_quantity
            pos["cost_basis_usd"] = total_cost
            pos["scaled_in"] = total_cost >= full_target_size - 1e-9
            actions.append({"type": "scale_in", "symbol": pos["symbol"], "pair": pair,
                             "size_usd": additional_needed, "fill_price": fill_price,
                             "partial": not pos["scaled_in"],
                             "reason": f"price confirmed momentum (+{args.scale_in_price_threshold_pct:.0%} from scout entry)"})

    # ---- consider new entries ----
    not_traded: dict[str, str] = {}
    if not allow_new_entries:
        not_traded = {pair: "regime filter blocking new entries (risk-OFF)"
                      for pair in eligible if pair not in state["positions"]}
    if allow_new_entries:
        open_slots = args.max_concurrent_positions - len(state["positions"])
        emerging_open = sum(1 for p in state["positions"].values() if p.get("tier") == "emerging")
        scout_open = sum(1 for p in state["positions"].values() if p.get("tier") == "scout")
        fresh_fetches_this_cycle = 0
        for pair, result in eligible.items():
            if pair in state["positions"]:
                continue
            if pair in sold_this_cycle:
                not_traded[pair] = "sold this same cycle -- not re-entering immediately (mirrors live's min_hours_between_trades_same_token)"
                continue
            if recently_stopped_out(state["closed_trades"], pair, cycle_start, args.min_hours_between_trades_same_token):
                not_traded[pair] = (f"stopped out within the last {args.min_hours_between_trades_same_token}h -- "
                                     f"cooldown before re-entering the same pair (mirrors live's "
                                     f"min_hours_between_trades_same_token)")
                continue
            if open_slots <= 0:
                not_traded[pair] = f"no open slots (max_concurrent_positions={args.max_concurrent_positions})"
                continue
            price = prices.get(pair)
            if price is None:
                not_traded[pair] = "no live price available"
                continue
            tier = result["tier"]
            if tier == "emerging" and emerging_open >= args.max_emerging_tier_positions:
                not_traded[pair] = f"emerging-tier cap reached (max_emerging_tier_positions={args.max_emerging_tier_positions})"
                continue
            if tier == "scout" and scout_open >= args.max_scout_positions:
                not_traded[pair] = f"scout-tier cap reached (max_scout_positions={args.max_scout_positions})"
                continue
            cached_closes = _load_price_history_cache(pair, args.history_days, interval_minutes=args.interval_minutes)
            if cached_closes is None and fresh_fetches_this_cycle >= MAX_FRESH_PRICE_HISTORY_FETCHES_PER_CYCLE:
                not_traded[pair] = (f"deferred to a future cycle (hit MAX_FRESH_PRICE_HISTORY_FETCHES_PER_CYCLE="
                                     f"{MAX_FRESH_PRICE_HISTORY_FETCHES_PER_CYCLE})")
                continue
            if cached_closes is None:
                fresh_fetches_this_cycle += 1
            closes = get_price_history_closes(pair, args.history_days, cached=cached_closes, interval_minutes=args.interval_minutes)
            signal = compute_signal(closes, args.strategy) if closes else None
            if signal != "buy" and closes is not None:
                not_traded[pair] = f"strategy signal was '{signal}', not buy"
                continue
            size_usd = size_position(tier, port_value, closes, args)
            is_scout = tier == "scout"
            if is_scout:
                size_usd = max(size_usd * args.scout_position_fraction, args.scout_min_trade_usd)
            elif size_usd < args.min_trade_usd:
                not_traded[pair] = f"sized position ${size_usd:.2f} below min_trade_usd (${args.min_trade_usd:.2f})"
                continue
            if size_usd > state["cash_usd"]:
                not_traded[pair] = f"sized position ${size_usd:.2f} exceeds available cash (${state['cash_usd']:.2f})"
                continue
            # Fee-aware edge check: IF this position hits its profit target,
            # would the gain still clear both legs' round-trip cost by a
            # healthy margin? A trade that only clears its own costs by a
            # hair isn't worth the tail risk of a worse-than-modeled fill
            # eating the rest -- see kraken/fees.py's edge_clears_costs()
            # and docs/STRATEGY.md "Fee-aware execution". Scout tier's
            # target is a MULTIPLE of cost basis (profit_take_multiple,
            # e.g. 2.0x = +100%), not a flat take_profit_pct, so it's
            # converted to the equivalent percentage here.
            target_pct = (args.profit_take_multiple - 1) if is_scout else args.take_profit_pct
            edge_check = kf.edge_clears_costs(
                size_usd, target_pct, entry_fee_bps=effective_taker_fee_bps, exit_fee_bps=effective_taker_fee_bps,
                entry_spread_bps=args.slippage_bps, min_edge_multiple=args.min_edge_to_cost_multiple,
            )
            if not edge_check["clears"]:
                not_traded[pair] = (f"edge-to-cost multiple {edge_check['edge_to_cost_multiple']:.2f}x < "
                                     f"min {args.min_edge_to_cost_multiple:.1f}x (round-trip cost "
                                     f"${edge_check['round_trip_cost_usd']:.3f} vs. ${edge_check['gross_profit_at_take_profit_usd']:.3f} "
                                     f"expected gross gain at target) -- not worth the fees even if the target hits")
                continue
            if tier in MEMECOIN_TIERS:
                current_exposure = memecoin_exposure_usd(state, prices)
                if current_exposure + size_usd > args.max_memecoin_exposure_fraction * port_value:
                    not_traded[pair] = (f"would exceed max_memecoin_exposure_fraction "
                                         f"(${current_exposure + size_usd:.2f} > "
                                         f"{args.max_memecoin_exposure_fraction:.0%} of ${port_value:.2f} portfolio)")
                    continue
            projected_heat = portfolio_heat_pct(state, prices, args.stop_loss_pct) + (size_usd * args.stop_loss_pct / port_value if port_value else 0)
            if projected_heat > args.max_portfolio_heat_pct:
                not_traded[pair] = f"would exceed max_portfolio_heat_pct ({projected_heat:.1%} > {args.max_portfolio_heat_pct:.1%})"
                continue
            fill_price = price * (1 + args.slippage_bps / 10_000)
            quantity = (size_usd * (1 - args.fee_bps / 10_000)) / fill_price
            state["cash_usd"] -= size_usd
            state["positions"][pair] = {
                "symbol": result["symbol"], "quantity": quantity, "entry_price_usd": fill_price,
                "entry_time": cycle_start.isoformat(), "tier": tier, "peak_price_usd": fill_price,
                "cost_basis_usd": size_usd, "scaled_in": not is_scout, "profit_taken": False,
            }
            actions.append({"type": "buy", "symbol": result["symbol"], "pair": pair, "tier": tier,
                             "size_usd": size_usd, "fill_price": fill_price,
                             "signal_basis": "buy signal" if closes else "no-history minimum-size entry"})
            open_slots -= 1
            if tier == "emerging":
                emerging_open += 1
            elif tier == "scout":
                scout_open += 1

    final_prices = dict(prices)
    missing = [pair for pair in state["positions"] if pair not in final_prices]
    if missing:
        final_prices.update(current_prices(missing))
        for pair in missing:
            final_prices.setdefault(pair, prices.get(pair, 0))
    final_value = portfolio_value_usd(state, final_prices)
    state["cycles_run"] += 1
    save_state(state)

    reported_before = final_value if not port_value_reliable else port_value

    append_journal({
        "timestamp": cycle_start.isoformat(),
        "type": "cycle",
        "portfolio_value_usd_before": reported_before,
        "portfolio_value_usd_before_pricing_incomplete": not port_value_reliable,
        "portfolio_value_usd_after": final_value,
        "regime_allows_new_entries": allow_new_entries,
        "discovery": {"found": len(candidates), "evaluated": len(pairs), "eligible": len(eligible), "rejected": rejected_count},
        "actions": actions,
        "not_traded": {pair: {"symbol": eligible[pair]["symbol"], "reason": reason} for pair, reason in not_traded.items()},
        "not_scaled_in": {pair: {"symbol": state["positions"][pair]["symbol"], "reason": reason}
                           for pair, reason in not_scaled_in.items()},
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
        for pair, reason in not_traded.items():
            print(f"  {eligible[pair]['symbol']:>10}  {reason}")
    if not_scaled_in:
        print(f"\nConfirmed momentum but not scaled in ({len(not_scaled_in)}):")
        for pair, reason in not_scaled_in.items():
            print(f"  {state['positions'][pair]['symbol']:>10}  {reason}")
    before_note = "  (pricing was incomplete at cycle start -- showing best available estimate, not a same-cycle move)" if not port_value_reliable else ""
    print(f"\nPortfolio value: ${reported_before:.2f} -> ${final_value:.2f}{before_note}  "
          f"(total return since start: {(final_value / state['starting_capital_usd'] - 1) * 100:+.1f}%)")
    print(f"Open positions: {len(state['positions'])}  |  Closed trades all-time: {len(state['closed_trades'])}")
    print(f"State saved to {STATE_PATH}")
    print(f"Journal appended to {JOURNAL_PATH}")


def disco_args(args) -> argparse.Namespace:
    """Adapts this script's CLI args into the shape
    research.discover_candidates.evaluate_candidate expects."""
    return argparse.Namespace(
        min_24h_volume_usd=args.min_24h_volume_usd,
        max_spread_bps=args.max_spread_bps,
        blue_chip_mcap_usd=args.blue_chip_mcap_usd,
        blue_chip_volume_usd=args.blue_chip_volume_usd,
        established_mcap_usd=args.established_mcap_usd,
        established_volume_usd=args.established_volume_usd,
    )


def scout_disco_args(args) -> argparse.Namespace:
    """A second, deliberately looser threshold set for very-illiquid pairs
    the normal thresholds structurally can't pass. PAPER-TRADING
    EXPERIMENTAL ONLY -- see the original pipeline's identical reasoning in
    git history. Does not touch config/discovery.yaml or
    research/discover_candidates.py's own CLI defaults."""
    base = disco_args(args)
    base.min_24h_volume_usd = args.scout_min_24h_volume_usd
    base.max_spread_bps = args.scout_max_spread_bps
    return base


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="Wipe paper state and restart at --starting-capital-usd")
    ap.add_argument("--starting-capital-usd", type=float, default=100.0,
                     help="Only takes effect on --reset (an existing state/paper_portfolio.json keeps its "
                          "original baseline regardless of this flag's value). Raised from 50.0 to 100.0 "
                          "2026-08-26 at the user's explicit request.")
    ap.add_argument("--max-candidates", type=int, default=700,
                     help="mirrors config/discovery.yaml's max_candidates_per_cycle -- set comfortably above "
                          "Kraken's current USD-pair count so this is a safety ceiling, not an active truncation.")
    ap.add_argument("--history-days", type=int, default=90)
    ap.add_argument("--interval-minutes", type=int, default=kc.OHLC_DAILY_INTERVAL_MINUTES,
                     choices=sorted(kc.OHLC_VALID_INTERVALS_MINUTES),
                     help="Bar granularity for signal generation. Default 1440 (daily, the original design). "
                          "2026-08-26 backtest evidence (docs/STRATEGY.md 'Day trading -- what the evidence "
                          "actually supports'): 60 (hourly) shows real, clean edge for several strategies -- "
                          "pair with --strategy ema_ribbon or donchian_channel_breakout. 15 and 5 (minute bars) "
                          "showed EVERY strategy losing to fee/slippage drag once real trade frequency is "
                          "accounted for -- the infrastructure supports them but the evidence says don't use "
                          "them yet. Does not change the regime filter, which stays on daily bars regardless "
                          "(a market-wide risk gate should track a slower timeframe than what it's gating).")
    ap.add_argument("--strategy", choices=list(strat.STRATEGIES), default="rsi_mean_reversion",
                     help="Which backtest/strategies.py strategy to paper-trade. Was adaptive_ensemble (still the "
                          "live default in config/risk.yaml/trade-cycle -- this flag does NOT change that, only "
                          "what gets paper-traded) until 2026-08-26: 170 live paper cycles produced zero trades "
                          "beyond the two opened in cycle 1, and a same-day backtest cut confirmed why -- "
                          "adaptive_ensemble only fires a real (non-drift) trade on 38.9%% of the live-eligible "
                          "universe over a full 180-day window, the lowest firing rate of any strategy with a "
                          "clean (non-unrealized-inflated) track record. rsi_mean_reversion fires on 54.5%% with "
                          "an equally clean +25.67%% avg return / 100%% win rate on real closed trades (0/18 "
                          "showed the unrealized-position inflation macd_crossover's headline number turned out "
                          "to be mostly made of) -- see docs/STRATEGY.md's 'Current strategies' for the full "
                          "comparison. This is a paper-trading default change based on real evidence, not yet a "
                          "live-trading recommendation -- CLAUDE.md rule 5 still requires this strategy build its "
                          "own real paper track record (not just a backtest) before it's a candidate for that.")
    # mirrors config/risk.yaml -- keep in sync by hand. max_position_fraction/max_position_usd raised
    # 2026-08-26 (0.30->0.40, $20->$50) alongside risk.yaml's matching bump, at the user's explicit request to
    # size more aggressively -- $50 = starting_capital_usd(100.0 default above) * risk.yaml's max_position_usd_pct
    # (0.50), same formula risk.yaml documents for live trading (see trade-cycle SKILL.md step 2); update this by
    # hand again if either the starting-capital default or that pct ever changes, same as before.
    ap.add_argument("--max-position-fraction", dest="max_position_fraction", type=float, default=0.40)
    ap.add_argument("--max-position-usd", dest="max_position_usd", type=float, default=50.0)
    ap.add_argument("--min-trade-usd", dest="min_trade_usd", type=float, default=5.0)
    ap.add_argument("--max-concurrent-positions", dest="max_concurrent_positions", type=int, default=15)
    ap.add_argument("--max-emerging-tier-positions", dest="max_emerging_tier_positions", type=int, default=2)
    ap.add_argument("--target-daily-volatility-pct", dest="target_daily_volatility_pct", type=float, default=3.0)
    ap.add_argument("--volatility-size-min-mult", dest="volatility_size_min_mult", type=float, default=0.5)
    ap.add_argument("--volatility-size-max-mult", dest="volatility_size_max_mult", type=float, default=1.5)
    ap.add_argument("--max-portfolio-heat-pct", dest="max_portfolio_heat_pct", type=float, default=0.12)
    ap.add_argument("--stop-loss-pct", dest="stop_loss_pct", type=float, default=0.15)
    ap.add_argument("--min-hours-between-trades-same-token", dest="min_hours_between_trades_same_token",
                     type=float, default=4.0)
    ap.add_argument("--take-profit-pct", dest="take_profit_pct", type=float, default=0.35)
    ap.add_argument("--trailing-stop-pct", dest="trailing_stop_pct", type=float, default=0.12)
    ap.add_argument("--scout-trailing-stop-pct", dest="scout_trailing_stop_pct", type=float, default=0.08)
    ap.add_argument("--house-money-trailing-stop-pct", dest="house_money_trailing_stop_pct", type=float, default=0.30)
    ap.add_argument("--circuit-breaker-floor-usd", dest="circuit_breaker_floor_usd", type=float, default=20.0)
    ap.add_argument("--circuit-breaker-daily-loss-pct", dest="circuit_breaker_daily_loss_pct", type=float, default=0.25)
    ap.add_argument("--regime-reference-pair", dest="regime_reference_pair", default="XBTUSD")
    ap.add_argument("--regime-sma-window-days", dest="regime_sma_window_days", type=int, default=30)
    ap.add_argument("--fee-bps", dest="fee_bps", type=float, default=26.0,
                     help="Kraken's default (non-VIP) taker fee is ~0.26%% -- more realistic than the old "
                          "Solana-DEX-shaped 30bps default, close enough not to bother separating further.")
    ap.add_argument("--slippage-bps", dest="slippage_bps", type=float, default=20.0,
                     help="Lower than the old 50bps default -- Kraken's major-pair spreads are typically much "
                          "tighter than a Solana DEX pool's price impact.")
    ap.add_argument("--min-edge-to-cost-multiple", dest="min_edge_to_cost_multiple", type=float, default=2.0,
                     help="Matches kraken/fees.py's edge_clears_costs() own default -- a position's expected "
                          "gross profit at its take-profit target must be at least this many multiples of the "
                          "round-trip fee+spread cost, or it's rejected as not worth the tail risk.")
    # discovery safety thresholds -- mirrors config/discovery.yaml
    ap.add_argument("--min-24h-volume-usd", dest="min_24h_volume_usd", type=float, default=1_000_000)
    ap.add_argument("--max-spread-bps", dest="max_spread_bps", type=float, default=50.0)
    ap.add_argument("--blue-chip-mcap-usd", dest="blue_chip_mcap_usd", type=float, default=50_000_000_000)
    ap.add_argument("--blue-chip-volume-usd", dest="blue_chip_volume_usd", type=float, default=100_000_000)
    ap.add_argument("--established-mcap-usd", dest="established_mcap_usd", type=float, default=1_000_000_000)
    ap.add_argument("--established-volume-usd", dest="established_volume_usd", type=float, default=10_000_000)
    # ---- Scout tier: tiered entry into very-illiquid pairs ------------
    ap.add_argument("--enable-scout-tier", dest="enable_scout_tier", action="store_true", default=True)
    ap.add_argument("--disable-scout-tier", dest="enable_scout_tier", action="store_false")
    ap.add_argument("--scout-min-24h-volume-usd", dest="scout_min_24h_volume_usd", type=float, default=100_000,
                     help="vs. min-24h-volume-usd's $1M -- a real but thinner pair can be legitimate with far "
                          "less volume")
    ap.add_argument("--scout-max-spread-bps", dest="scout_max_spread_bps", type=float, default=150.0,
                     help="vs. max-spread-bps's 50 -- still capped well below 'this pair is barely tradable'")
    ap.add_argument("--scout-position-fraction", dest="scout_position_fraction", type=float, default=0.30,
                     help="Raised from 0.20 2026-08-26 at the user's explicit request to size more aggressively. "
                          "In practice --scout-min-trade-usd's floor still dominates most scout sizing at typical "
                          "portfolio values -- this mostly matters as the portfolio grows.")
    ap.add_argument("--scout-min-trade-usd", dest="scout_min_trade_usd", type=float, default=5.0,
                     help="Raised from 2.5 2026-08-26 (matches --min-trade-usd's floor) -- every scout buy so far "
                          "has landed on this floor (base tier-scaled size before it was well under $2.50), so "
                          "this is the lever that actually controls scout-tier trade size today, not the fraction "
                          "above.")
    ap.add_argument("--scale-in-price-threshold-pct", dest="scale_in_price_threshold_pct", type=float, default=0.15)
    ap.add_argument("--profit-take-multiple", dest="profit_take_multiple", type=float, default=2.0)
    ap.add_argument("--max-scout-positions", dest="max_scout_positions", type=int, default=8)
    ap.add_argument("--max-memecoin-exposure-fraction", dest="max_memecoin_exposure_fraction", type=float, default=0.40,
                     help="Raised from 0.20 2026-08-26 at the user's explicit request to size more aggressively. "
                          "This is the cap that actually governs how many scout positions can coexist in "
                          "practice -- at the current $5 scout-min-trade-usd floor and $100 starting capital, "
                          "0.40 allows roughly 8 scout positions before this (not --max-scout-positions) binds, "
                          "so --max-scout-positions was intentionally left at 8 rather than also raised: raising "
                          "it further without raising this would have been a no-op.")
    args = ap.parse_args()
    with CycleLock(LOCK_PATH):
        run_cycle(args)


if __name__ == "__main__":
    main()
