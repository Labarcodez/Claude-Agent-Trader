"""Unit tests for paper_trading/run_paper_cycle.py's pure sizing/risk logic
-- synthetic state and price dicts, no network calls, no state file I/O.
Run: python3 -m unittest discover -s tests -v"""
import argparse
import json
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paper_trading import run_paper_cycle as p  # noqa: E402


def make_size_args(**overrides):
    defaults = dict(
        max_position_usd=20.0,
        max_position_fraction=0.30,
        min_trade_usd=5.0,
        target_daily_volatility_pct=3.0,
        volatility_size_min_mult=0.5,
        volatility_size_max_mult=1.5,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestTierMultipliers(unittest.TestCase):
    def test_matches_config_discovery_yaml(self):
        # config/discovery.yaml's tiers block is the source of truth for
        # these three; this test exists so a change to one doesn't silently
        # drift from the other. "scout" is deliberately NOT in
        # config/discovery.yaml -- it's a paper-trading-only experimental
        # tier (see scout_disco_args()'s docstring), so it's excluded here
        # rather than asserted against a config block it isn't sourced from.
        live_tiers = {k: v for k, v in p.TIER_MULTIPLIERS.items() if k != "scout"}
        self.assertEqual(live_tiers, {"blue_chip": 1.0, "established": 0.7, "emerging": 0.4})

    def test_scout_tier_exists_for_the_experimental_pathway(self):
        self.assertIn("scout", p.TIER_MULTIPLIERS)


class TestPortfolioValueUsd(unittest.TestCase):
    def test_cash_only(self):
        state = {"cash_usd": 50.0, "positions": {}}
        self.assertEqual(p.portfolio_value_usd(state, {}), 50.0)

    def test_cash_plus_positions(self):
        state = {"cash_usd": 10.0, "positions": {"XBTUSD": {"quantity": 2.0}, "ETHUSD": {"quantity": 5.0}}}
        prices = {"XBTUSD": 3.0, "ETHUSD": 1.0}
        self.assertAlmostEqual(p.portfolio_value_usd(state, prices), 10.0 + 2.0 * 3.0 + 5.0 * 1.0)

    def test_missing_price_excludes_position_rather_than_erroring(self):
        state = {"cash_usd": 10.0, "positions": {"XBTUSD": {"quantity": 2.0}}}
        self.assertEqual(p.portfolio_value_usd(state, {}), 10.0)


class TestAllPositionsPriced(unittest.TestCase):
    """all_positions_priced() gates whether portfolio_value_usd()'s result is
    trustworthy for circuit-breaker/peak-tracking decisions -- a real false
    circuit-breaker trip happened when a single failed batched pricing
    request silently zeroed out every open position's price at once,
    collapsing portfolio_value_usd() to cash-only and looking exactly like a
    33% drawdown that never actually happened (true value moments later,
    independently verified, was down only 1.4% from peak)."""

    def test_true_with_no_open_positions(self):
        state = {"positions": {}}
        self.assertTrue(p.all_positions_priced(state, {}))

    def test_true_when_every_position_has_a_price(self):
        state = {"positions": {"XBTUSD": {}, "ETHUSD": {}}}
        self.assertTrue(p.all_positions_priced(state, {"XBTUSD": 1.0, "ETHUSD": 2.0}))

    def test_false_when_any_position_is_missing_a_price(self):
        state = {"positions": {"XBTUSD": {}, "ETHUSD": {}}}
        self.assertFalse(p.all_positions_priced(state, {"XBTUSD": 1.0}))

    def test_false_when_all_positions_are_missing_prices(self):
        # the exact real failure mode: one failed batch request wipes every
        # open position's price at once, not just one of several
        state = {"positions": {"XBTUSD": {}, "ETHUSD": {}}}
        self.assertFalse(p.all_positions_priced(state, {}))


class TestPortfolioHeatPct(unittest.TestCase):
    def test_zero_with_no_positions(self):
        state = {"cash_usd": 50.0, "positions": {}}
        self.assertEqual(p.portfolio_heat_pct(state, {}, stop_loss_pct=0.15), 0.0)

    def test_zero_when_portfolio_value_is_zero(self):
        state = {"cash_usd": 0.0, "positions": {}}
        self.assertEqual(p.portfolio_heat_pct(state, {}, stop_loss_pct=0.15), 0.0)

    def test_heat_scales_with_position_size_and_stop_loss(self):
        # $20 position at a 15% stop-loss on a $40 portfolio -> heat_usd=$3, heat=7.5%
        state = {"cash_usd": 20.0, "positions": {"XBTUSD": {"quantity": 2.0}}}
        prices = {"XBTUSD": 10.0}
        heat = p.portfolio_heat_pct(state, prices, stop_loss_pct=0.15)
        self.assertAlmostEqual(heat, 3.0 / 40.0, places=6)

    def test_multiple_positions_sum(self):
        state = {"cash_usd": 0.0, "positions": {
            "A": {"quantity": 1.0}, "B": {"quantity": 1.0},
        }}
        prices = {"A": 10.0, "B": 10.0}
        # portfolio value = 20, heat_usd = (10*0.1)+(10*0.1) = 2, heat = 2/20 = 0.10
        heat = p.portfolio_heat_pct(state, prices, stop_loss_pct=0.10)
        self.assertAlmostEqual(heat, 0.10, places=6)


class TestComputePartialProfitTake(unittest.TestCase):
    """compute_partial_profit_take() implements 'sell 100% of initial
    investment after a 2x-3x gain, let the remainder ride' -- scout tier's
    tiered profit-taking rule."""

    def test_none_below_the_multiple(self):
        # $10 cost basis, 100 units, price $0.15 -> value $15 = 1.5x, below a 2.5x trigger
        self.assertIsNone(p.compute_partial_profit_take(10.0, 100.0, 0.15, profit_take_multiple=2.5))

    def test_none_with_zero_or_negative_cost_basis(self):
        # already taken (cost_basis reset to 0), or a pre-existing position with no tracked cost basis
        self.assertIsNone(p.compute_partial_profit_take(0.0, 100.0, 1.0, profit_take_multiple=2.5))

    def test_triggers_at_exactly_the_multiple(self):
        # $10 cost basis, price such that value = exactly 2.5x = $25
        result = p.compute_partial_profit_take(10.0, 100.0, price=0.25, profit_take_multiple=2.5)
        self.assertIsNotNone(result)

    def test_sells_exactly_enough_to_recoup_cost_basis(self):
        # $10 cost basis, 100 units, price $0.30 (value $30 = 3x) -> should sell
        # cost_basis/price = 10/0.30 = 33.33 units to recoup exactly $10
        quantity = p.compute_partial_profit_take(10.0, 100.0, price=0.30, profit_take_multiple=2.5)
        self.assertAlmostEqual(quantity, 10.0 / 0.30, places=6)
        self.assertAlmostEqual(quantity * 0.30, 10.0, places=6)  # proceeds == cost basis, by construction

    def test_never_sells_more_than_held(self):
        # a huge price move means cost_basis/price would be tiny -- but confirm
        # the cap holds even in a contrived case where it wouldn't naturally
        tiny_quantity = 0.001
        result = p.compute_partial_profit_take(10.0, tiny_quantity, price=100_000.0, profit_take_multiple=2.5)
        self.assertLessEqual(result, tiny_quantity)


class TestComputeScaleInTopup(unittest.TestCase):
    """compute_scale_in_topup() -- the sizing decision for a scout position's
    momentum-confirmed scale-in. Split out of run_cycle's scale-in loop so
    it's unit-testable without mocking the whole cycle, and caps to whatever
    room fits instead of an all-or-nothing skip -- exposure has repeatedly
    sat at/near the cap live."""

    def test_already_full_when_gap_below_min_trade(self):
        amount, reason = p.compute_scale_in_topup(
            cost_basis_usd=9.7, full_target_size=10.0, cash_usd=100.0,
            exposure_room_usd=100.0, min_trade_usd=1.0)
        self.assertEqual(amount, 0.0)
        self.assertEqual(reason, "already_full")

    def test_full_topup_when_room_is_ample(self):
        amount, reason = p.compute_scale_in_topup(
            cost_basis_usd=2.0, full_target_size=10.0, cash_usd=100.0,
            exposure_room_usd=100.0, min_trade_usd=1.0)
        self.assertIsNone(reason)
        self.assertAlmostEqual(amount, 8.0)

    def test_capped_by_exposure_room_partial_topup(self):
        # wants $8 more but only $3 of exposure room remains -- should top up
        # by exactly the $3 that fits rather than skip entirely
        amount, reason = p.compute_scale_in_topup(
            cost_basis_usd=2.0, full_target_size=10.0, cash_usd=100.0,
            exposure_room_usd=3.0, min_trade_usd=1.0)
        self.assertIsNone(reason)
        self.assertAlmostEqual(amount, 3.0)

    def test_capped_by_cash_partial_topup(self):
        amount, reason = p.compute_scale_in_topup(
            cost_basis_usd=2.0, full_target_size=10.0, cash_usd=2.5,
            exposure_room_usd=100.0, min_trade_usd=1.0)
        self.assertIsNone(reason)
        self.assertAlmostEqual(amount, 2.5)

    def test_insufficient_room_when_capped_amount_below_min_trade(self):
        amount, reason = p.compute_scale_in_topup(
            cost_basis_usd=2.0, full_target_size=10.0, cash_usd=100.0,
            exposure_room_usd=0.50, min_trade_usd=1.0)
        self.assertEqual(amount, 0.0)
        self.assertEqual(reason, "insufficient_room")

    def test_negative_exposure_room_treated_as_zero_not_negative(self):
        # exposure already over the cap -- should behave like zero room
        # available, not go negative and pass some nonsensical min() result
        amount, reason = p.compute_scale_in_topup(
            cost_basis_usd=2.0, full_target_size=10.0, cash_usd=100.0,
            exposure_room_usd=-5.0, min_trade_usd=1.0)
        self.assertEqual(amount, 0.0)
        self.assertEqual(reason, "insufficient_room")


class TestCycleLock(unittest.TestCase):
    """CycleLock guards a paper-trading cycle against a second concurrent
    run_paper_cycle.py process (e.g. this session's cron loop and a separate
    local terminal loop both pointed at the same state/journal files -- a
    real setup, not hypothetical)."""

    def setUp(self):
        self.lock_path = Path(__file__).resolve().parent / "_tmp_cycle_lock_test.lock"
        if self.lock_path.exists():
            self.lock_path.unlink()

    def tearDown(self):
        if self.lock_path.exists():
            self.lock_path.unlink()

    def test_acquires_and_releases_cleanly(self):
        with p.CycleLock(self.lock_path):
            self.assertTrue(self.lock_path.exists())
        self.assertFalse(self.lock_path.exists(), "lock file must be removed on exit")

    def test_second_acquire_blocks_until_first_releases(self):
        import threading
        order = []
        lock = p.CycleLock(self.lock_path, timeout=5.0, poll=0.02)
        with lock:
            def try_acquire():
                with p.CycleLock(self.lock_path, timeout=5.0, poll=0.02):
                    order.append("second")
            t = threading.Thread(target=try_acquire)
            t.start()
            time.sleep(0.15)  # give the second thread a chance to (wrongly) sneak in
            order.append("first-still-holding")
            t.join(timeout=5.0)
        self.assertEqual(order, ["first-still-holding", "second"],
                          "a concurrent acquire must wait for the held lock, not proceed immediately")

    def test_stale_lock_is_broken_after_timeout(self):
        # simulate a lock left behind by a crashed process -- must not deadlock forever
        self.lock_path.write_text("")
        start = time.time()
        with p.CycleLock(self.lock_path, timeout=0.2, poll=0.05):
            pass
        self.assertLess(time.time() - start, 5.0, "a stale lock should be broken quickly, not hang")


class TestRecentlyStoppedOut(unittest.TestCase):
    """recently_stopped_out() -- the stop-loss re-entry cooldown, mirroring
    config/risk.yaml's min_hours_between_trades_same_token."""

    def test_true_within_cooldown_window(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        closed_trades = [{"pair": "XBTUSD", "reason": "stop-loss (-15.0%)",
                           "closed_at": (now - timedelta(hours=2)).isoformat()}]
        self.assertTrue(p.recently_stopped_out(closed_trades, "XBTUSD", now, cooldown_hours=4.0))

    def test_false_once_cooldown_window_has_passed(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        closed_trades = [{"pair": "XBTUSD", "reason": "stop-loss (-15.0%)",
                           "closed_at": (now - timedelta(hours=5)).isoformat()}]
        self.assertFalse(p.recently_stopped_out(closed_trades, "XBTUSD", now, cooldown_hours=4.0))

    def test_false_for_a_different_pair(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        closed_trades = [{"pair": "OTHERUSD", "reason": "stop-loss (-15.0%)",
                           "closed_at": (now - timedelta(minutes=5)).isoformat()}]
        self.assertFalse(p.recently_stopped_out(closed_trades, "XBTUSD", now, cooldown_hours=4.0))

    def test_false_for_a_non_stop_loss_exit(self):
        # take-profit / trailing-stop are good outcomes -- only a stop-loss
        # exit should trigger a cooldown before re-entering the same pair
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        for reason in ("take-profit (+35.0%)", "trailing-stop (-12.0% from peak)", "strategy sell signal"):
            with self.subTest(reason=reason):
                closed_trades = [{"pair": "XBTUSD", "reason": reason,
                                   "closed_at": (now - timedelta(minutes=5)).isoformat()}]
                self.assertFalse(p.recently_stopped_out(closed_trades, "XBTUSD", now, cooldown_hours=4.0))

    def test_malformed_closed_at_degrades_to_false_not_a_crash(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        closed_trades = [{"pair": "XBTUSD", "reason": "stop-loss (-15.0%)", "closed_at": "not-a-timestamp"}]
        self.assertFalse(p.recently_stopped_out(closed_trades, "XBTUSD", now, cooldown_hours=4.0))

    def test_empty_closed_trades(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(p.recently_stopped_out([], "XBTUSD", now, cooldown_hours=4.0))


class TestMemecoinExposureUsd(unittest.TestCase):
    """memecoin_exposure_usd() sums scout+emerging tier value -- what
    max_memecoin_exposure_fraction caps ('risk a maximum of X% of total
    portfolio on low-cap/low-volume pairs')."""

    def test_zero_with_no_positions(self):
        state = {"positions": {}}
        self.assertEqual(p.memecoin_exposure_usd(state, {}), 0.0)

    def test_includes_scout_and_emerging_only(self):
        state = {"positions": {
            "SCOUT1": {"quantity": 10.0, "tier": "scout"},
            "EMERGE1": {"quantity": 5.0, "tier": "emerging"},
            "BLUE1": {"quantity": 1.0, "tier": "blue_chip"},
            "ESTAB1": {"quantity": 2.0, "tier": "established"},
        }}
        prices = {"SCOUT1": 1.0, "EMERGE1": 2.0, "BLUE1": 100.0, "ESTAB1": 50.0}
        # scout: 10*1=10, emerging: 5*2=10 -> 20 total, blue_chip/established excluded
        self.assertEqual(p.memecoin_exposure_usd(state, prices), 20.0)

    def test_excludes_unpriced_positions_rather_than_erroring(self):
        state = {"positions": {"SCOUT1": {"quantity": 10.0, "tier": "scout"}}}
        self.assertEqual(p.memecoin_exposure_usd(state, {}), 0.0)


class TestScoutDiscoArgs(unittest.TestCase):
    """scout_disco_args() loosens the exit-liquidity/spread thresholds for
    very-illiquid pairs -- Kraken has no on-chain safety checks to keep
    unchanged the way the old Solana pipeline did (there's nothing here
    equivalent to a mint/freeze-authority check), so this is a simpler
    loosen-only adapter than the old pipeline's version."""

    def _base_args(self):
        return argparse.Namespace(
            min_24h_volume_usd=1_000_000, max_spread_bps=50.0,
            blue_chip_mcap_usd=50_000_000_000, blue_chip_volume_usd=100_000_000,
            established_mcap_usd=1_000_000_000, established_volume_usd=10_000_000,
            scout_min_24h_volume_usd=100_000, scout_max_spread_bps=150.0,
        )

    def test_thresholds_are_looser_than_normal(self):
        scout = p.scout_disco_args(self._base_args())
        self.assertLess(scout.min_24h_volume_usd, 1_000_000)
        self.assertGreater(scout.max_spread_bps, 50.0)

    def test_tier_thresholds_are_unchanged(self):
        base = self._base_args()
        scout = p.scout_disco_args(base)
        self.assertEqual(scout.blue_chip_mcap_usd, base.blue_chip_mcap_usd)
        self.assertEqual(scout.established_mcap_usd, base.established_mcap_usd)


class TestSizePosition(unittest.TestCase):
    def test_no_history_uses_minimum_size_only(self):
        # trade-cycle's documented rule: no vol-scaling for a pair with no
        # backtestable history -- capped near min_trade_usd regardless of tier/portfolio size
        size = p.size_position("blue_chip", portfolio_value=1000.0, closes=None, args=make_size_args())
        self.assertLessEqual(size, make_size_args().min_trade_usd * 2)
        self.assertGreaterEqual(size, make_size_args().min_trade_usd)

    def test_tier_multiplier_scales_size(self):
        # flat closes -> zero realized vol -> mult=1.0, so the tier multiplier
        # is the only thing differentiating these
        flat = [100.0] * 25
        args = make_size_args()
        blue_chip = p.size_position("blue_chip", 100.0, flat, args)
        established = p.size_position("established", 100.0, flat, args)
        emerging = p.size_position("emerging", 100.0, flat, args)
        self.assertGreater(blue_chip, established)
        self.assertGreater(established, emerging)

    def test_higher_volatility_shrinks_size(self):
        args = make_size_args()
        calm = [100.0, 100.1, 99.9, 100.2, 99.8] * 5
        wild = [100.0, 130.0, 70.0, 140.0, 60.0] * 5
        calm_size = p.size_position("blue_chip", 100.0, calm, args)
        wild_size = p.size_position("blue_chip", 100.0, wild, args)
        self.assertGreaterEqual(calm_size, wild_size)

    def test_respects_max_position_usd_cap(self):
        args = make_size_args(max_position_usd=5.0, max_position_fraction=0.9)
        flat = [100.0] * 25
        size = p.size_position("blue_chip", 1000.0, flat, args)
        self.assertLessEqual(size, 5.0)

    def test_respects_max_position_fraction(self):
        args = make_size_args(max_position_usd=1000.0, max_position_fraction=0.1)
        flat = [100.0] * 25
        size = p.size_position("blue_chip", 100.0, flat, args)
        self.assertLessEqual(size, 100.0 * 0.1 + 1e-9)


class TestRegimeCache(unittest.TestCase):
    """regime_allows_new_entries() caches its result for
    REGIME_CACHE_TTL_SECONDS instead of hitting Kraken's OHLC endpoint every
    paper cycle -- a 30-day SMA doesn't meaningfully change in 15 minutes."""

    def setUp(self):
        self._orig_path = p.REGIME_CACHE_PATH
        p.REGIME_CACHE_PATH = Path(__file__).resolve().parent / "_tmp_regime_cache_test.json"
        if p.REGIME_CACHE_PATH.exists():
            p.REGIME_CACHE_PATH.unlink()

    def tearDown(self):
        if p.REGIME_CACHE_PATH.exists():
            p.REGIME_CACHE_PATH.unlink()
        p.REGIME_CACHE_PATH = self._orig_path

    @patch("backtest.fetch_history.fetch_ohlc_kraken")
    def test_fetches_live_on_empty_cache_then_caches_it(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(40)]}
        result1 = p.regime_allows_new_entries("XBTUSD", 30)
        result2 = p.regime_allows_new_entries("XBTUSD", 30)
        self.assertEqual(mock_fetch.call_count, 1, "second call within TTL should use the cache, not refetch")
        self.assertEqual(result1, result2)

    @patch("backtest.fetch_history.fetch_ohlc_kraken")
    def test_different_reference_pair_bypasses_stale_cache_entry(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(40)]}
        p.regime_allows_new_entries("XBTUSD", 30)
        p.regime_allows_new_entries("ETHUSD", 30)
        self.assertEqual(mock_fetch.call_count, 2, "a different pair must not reuse another pair's cached read")

    @patch("backtest.fetch_history.fetch_ohlc_kraken")
    def test_expired_cache_entry_triggers_a_refetch(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(40)]}
        p.regime_allows_new_entries("XBTUSD", 30)
        # backdate the cache past the TTL
        cached = json.loads(p.REGIME_CACHE_PATH.read_text())
        stale = datetime.now(timezone.utc) - timedelta(seconds=p.REGIME_CACHE_TTL_SECONDS + 1)
        cached["computed_at"] = stale.isoformat()
        p.REGIME_CACHE_PATH.write_text(json.dumps(cached))
        p.regime_allows_new_entries("XBTUSD", 30)
        self.assertEqual(mock_fetch.call_count, 2)

    def test_malformed_but_valid_json_cache_degrades_to_none_not_a_crash(self):
        """Real bug found live in the old pipeline: a cache file that's valid
        JSON but missing computed_at (partial write, disk issue, manual edit)
        raised an uncaught KeyError, crashing the whole cycle instead of
        falling back to a live fetch."""
        p.REGIME_CACHE_PATH.write_text(json.dumps({
            "reference_pair": "XBTUSD", "sma_window_days": 30, "allows_new_entries": True,
        }))  # missing computed_at
        self.assertIsNone(p._load_regime_cache("XBTUSD", 30))

    def test_non_iso_computed_at_degrades_to_none_not_a_crash(self):
        p.REGIME_CACHE_PATH.write_text(json.dumps({
            "reference_pair": "XBTUSD", "sma_window_days": 30, "allows_new_entries": True,
            "computed_at": "not-a-real-timestamp",
        }))
        self.assertIsNone(p._load_regime_cache("XBTUSD", 30))


class TestPriceHistoryCache(unittest.TestCase):
    """get_price_history_closes() caches per (pair, days) for
    PRICE_HISTORY_CACHE_TTL_SECONDS -- with several eligible candidates per
    cycle, this avoids hitting Kraken's OHLC endpoint once per candidate
    every cycle for data that barely changes within 15 minutes."""

    def setUp(self):
        self._orig_dir = p.PRICE_HISTORY_CACHE_DIR
        p.PRICE_HISTORY_CACHE_DIR = Path(__file__).resolve().parent / "_tmp_price_history_cache_test"
        if p.PRICE_HISTORY_CACHE_DIR.exists():
            for f in p.PRICE_HISTORY_CACHE_DIR.iterdir():
                f.unlink()
            p.PRICE_HISTORY_CACHE_DIR.rmdir()

    def tearDown(self):
        if p.PRICE_HISTORY_CACHE_DIR.exists():
            for f in p.PRICE_HISTORY_CACHE_DIR.iterdir():
                f.unlink()
            p.PRICE_HISTORY_CACHE_DIR.rmdir()
        p.PRICE_HISTORY_CACHE_DIR = self._orig_dir

    @patch("backtest.fetch_history.fetch_ohlc_kraken")
    def test_second_call_within_ttl_skips_the_network(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(20)]}
        closes1 = p.get_price_history_closes("XBTUSD", 90)
        closes2 = p.get_price_history_closes("XBTUSD", 90)
        self.assertEqual(mock_fetch.call_count, 1)
        self.assertEqual(closes1, closes2)

    @patch("backtest.fetch_history.fetch_ohlc_kraken")
    def test_uses_an_impatient_retry_policy_not_the_backtest_default(self, mock_fetch):
        """This runs inside a tight cron loop, not a one-off backtest -- a
        candidate this gives up on quickly just gets reconsidered next
        cycle, so failing fast here is the right trade."""
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(20)]}
        p.get_price_history_closes("XBTUSD", 90)
        _, kwargs = mock_fetch.call_args
        self.assertLessEqual(kwargs.get("retries", 3), 2)
        self.assertLessEqual(kwargs.get("backoff", 2.0), 1.5)

    @patch("backtest.fetch_history.fetch_ohlc_kraken")
    def test_different_pair_is_not_served_from_another_pairs_cache(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(20)]}
        p.get_price_history_closes("XBTUSD", 90)
        p.get_price_history_closes("ETHUSD", 90)
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("backtest.fetch_history.fetch_ohlc_kraken")
    def test_expired_cache_entry_triggers_a_refetch(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(20)]}
        p.get_price_history_closes("XBTUSD", 90)
        cache_path = p._price_history_cache_path("XBTUSD", 90)
        cached = json.loads(cache_path.read_text())
        stale = datetime.now(timezone.utc) - timedelta(seconds=p.PRICE_HISTORY_CACHE_TTL_SECONDS + 1)
        cached["computed_at"] = stale.isoformat()
        cache_path.write_text(json.dumps(cached))
        p.get_price_history_closes("XBTUSD", 90)
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("backtest.fetch_history.fetch_ohlc_kraken")
    def test_insufficient_history_is_not_cached(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(5)]}  # under the 15-point minimum
        result = p.get_price_history_closes("XBTUSD", 90)
        self.assertIsNone(result)

    def test_malformed_but_valid_json_cache_degrades_to_none_not_a_crash(self):
        """Same real bug as TestRegimeCache's equivalent test, in the sibling
        cache function."""
        p._price_history_cache_path("XBTUSD", 90).parent.mkdir(parents=True, exist_ok=True)
        p._price_history_cache_path("XBTUSD", 90).write_text(json.dumps({"closes": [1.0, 2.0]}))  # missing computed_at
        self.assertIsNone(p._load_price_history_cache("XBTUSD", 90))

    @patch("backtest.fetch_history.fetch_ohlc_kraken")
    def test_caller_supplied_cached_value_skips_the_internal_lookup(self, mock_fetch):
        """get_price_history_closes(pair, days, cached=...) lets a caller that
        already did its own _load_price_history_cache() lookup (e.g. to
        decide whether this fetch counts against a per-cycle budget) pass the
        result straight through instead of the function re-reading and
        re-parsing the same cache file a second time."""
        with patch.object(p, "_load_price_history_cache") as mock_load:
            result = p.get_price_history_closes("XBTUSD", 90, cached=[1.0, 2.0, 3.0])
            mock_load.assert_not_called()
        self.assertEqual(result, [1.0, 2.0, 3.0])
        mock_fetch.assert_not_called()
        self.assertFalse(p._price_history_cache_path("XBTUSD", 90).exists())


class TestCurrentPrices(unittest.TestCase):
    """current_prices() sources mid ((ask+bid)/2) prices from Kraken's Ticker
    endpoint via kraken/client.py's ticker(), which already batches pairs
    into as few requests as possible -- see kraken/client.py's CHUNK_SIZE."""

    @patch("kraken.client.ticker")
    def test_reads_mid_price_from_ask_and_bid(self, mock_ticker):
        mock_ticker.return_value = {
            "XBTUSD": {"a": ["100.0", "1", "1"], "b": ["98.0", "1", "1"], "c": ["99.0", "1"]},
        }
        prices = p.current_prices(["XBTUSD"])
        self.assertEqual(prices, {"XBTUSD": 99.0})

    @patch("kraken.client.ticker")
    def test_falls_back_to_last_trade_price_when_no_ask_or_bid(self, mock_ticker):
        mock_ticker.return_value = {
            "XBTUSD": {"a": ["0", "1", "1"], "b": ["0", "1", "1"], "c": ["97.5", "1"]},
        }
        prices = p.current_prices(["XBTUSD"])
        self.assertEqual(prices, {"XBTUSD": 97.5})

    @patch("kraken.client.ticker")
    def test_pair_absent_from_response_is_simply_missing(self, mock_ticker):
        mock_ticker.return_value = {"XBTUSD": {"a": ["100.0", "1", "1"], "b": ["98.0", "1", "1"], "c": ["99.0", "1"]}}
        prices = p.current_prices(["XBTUSD", "ETHUSD"])
        self.assertEqual(prices, {"XBTUSD": 99.0})
        self.assertNotIn("ETHUSD", prices)

    @patch("kraken.client.ticker")
    def test_empty_pair_list_short_circuits_without_calling_ticker(self, mock_ticker):
        prices = p.current_prices([])
        self.assertEqual(prices, {})
        mock_ticker.assert_not_called()

    @patch("kraken.client.ticker")
    def test_kraken_api_error_degrades_to_empty_rather_than_crashing(self, mock_ticker):
        from kraken.client import KrakenAPIError
        mock_ticker.side_effect = KrakenAPIError("boom")
        self.assertEqual(p.current_prices(["XBTUSD"]), {})

    @patch("kraken.client.ticker")
    def test_malformed_ticker_entry_is_skipped_not_a_crash(self, mock_ticker):
        mock_ticker.return_value = {"XBTUSD": {"a": ["not-a-number"], "b": ["98.0"], "c": ["99.0", "1"]}}
        self.assertEqual(p.current_prices(["XBTUSD"]), {})


if __name__ == "__main__":
    unittest.main()
