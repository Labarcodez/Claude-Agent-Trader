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
        state = {"cash_usd": 10.0, "positions": {"MINT1": {"quantity": 2.0}, "MINT2": {"quantity": 5.0}}}
        prices = {"MINT1": 3.0, "MINT2": 1.0}
        self.assertAlmostEqual(p.portfolio_value_usd(state, prices), 10.0 + 2.0 * 3.0 + 5.0 * 1.0)

    def test_missing_price_excludes_position_rather_than_erroring(self):
        state = {"cash_usd": 10.0, "positions": {"MINT1": {"quantity": 2.0}}}
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
        state = {"positions": {"MINT1": {}, "MINT2": {}}}
        self.assertTrue(p.all_positions_priced(state, {"MINT1": 1.0, "MINT2": 2.0}))

    def test_false_when_any_position_is_missing_a_price(self):
        state = {"positions": {"MINT1": {}, "MINT2": {}}}
        self.assertFalse(p.all_positions_priced(state, {"MINT1": 1.0}))

    def test_false_when_all_positions_are_missing_prices(self):
        # the exact real failure mode: one failed batch request wipes every
        # open position's price at once, not just one of several
        state = {"positions": {"MINT1": {}, "MINT2": {}}}
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
        state = {"cash_usd": 20.0, "positions": {"MINT1": {"quantity": 2.0}}}
        prices = {"MINT1": 10.0}
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
        # $10 cost basis, 100 tokens, price $0.15 -> value $15 = 1.5x, below a 2.5x trigger
        self.assertIsNone(p.compute_partial_profit_take(10.0, 100.0, 0.15, profit_take_multiple=2.5))

    def test_none_with_zero_or_negative_cost_basis(self):
        # already taken (cost_basis reset to 0), or a pre-existing position with no tracked cost basis
        self.assertIsNone(p.compute_partial_profit_take(0.0, 100.0, 1.0, profit_take_multiple=2.5))

    def test_triggers_at_exactly_the_multiple(self):
        # $10 cost basis, price such that value = exactly 2.5x = $25
        result = p.compute_partial_profit_take(10.0, 100.0, price=0.25, profit_take_multiple=2.5)
        self.assertIsNotNone(result)

    def test_sells_exactly_enough_to_recoup_cost_basis(self):
        # $10 cost basis, 100 tokens, price $0.30 (value $30 = 3x) -> should sell
        # cost_basis/price = 10/0.30 = 33.33 tokens to recoup exactly $10
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
    momentum-confirmed scale-in. Split out of run_cycle's scale-in loop after
    mining the journal found 383+ cycles with zero logged scale_in actions
    despite a position (GIKO) already showing scaled_in=True live: the old
    inline 'too small to bother' branch silently flipped the flag without
    buying or logging anything. This also covers the related fix -- capping
    to whatever room fits instead of an all-or-nothing skip -- since exposure
    has repeatedly sat at/near the cap live."""

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
    real setup, not hypothetical). Mining the journal found two
    near-simultaneous buy/sell pairs on the same symbol (MET, RIZO), seconds
    apart, identical size and return_pct -- the signature of two processes
    each reading the same pre-trade state and independently making the same
    decision before either had written back."""

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
    config/risk.yaml's min_hours_between_trades_same_token. Added after
    mining the journal found CEZ stopped out, was re-bought only ~2 hours
    later (well under the configured 4h), and immediately stopped out again
    -- running two independent loops against the same state meant real
    cadence between cycles was faster than either loop's own interval."""

    def test_true_within_cooldown_window(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        closed_trades = [{"mint": "X", "reason": "stop-loss (-15.0%)",
                           "closed_at": (now - timedelta(hours=2)).isoformat()}]
        self.assertTrue(p.recently_stopped_out(closed_trades, "X", now, cooldown_hours=4.0))

    def test_false_once_cooldown_window_has_passed(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        closed_trades = [{"mint": "X", "reason": "stop-loss (-15.0%)",
                           "closed_at": (now - timedelta(hours=5)).isoformat()}]
        self.assertFalse(p.recently_stopped_out(closed_trades, "X", now, cooldown_hours=4.0))

    def test_false_for_a_different_mint(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        closed_trades = [{"mint": "OTHER", "reason": "stop-loss (-15.0%)",
                           "closed_at": (now - timedelta(minutes=5)).isoformat()}]
        self.assertFalse(p.recently_stopped_out(closed_trades, "X", now, cooldown_hours=4.0))

    def test_false_for_a_non_stop_loss_exit(self):
        # take-profit / trailing-stop are good outcomes -- only a stop-loss
        # exit should trigger a cooldown before re-entering the same token
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        for reason in ("take-profit (+35.0%)", "trailing-stop (-12.0% from peak)", "strategy sell signal"):
            with self.subTest(reason=reason):
                closed_trades = [{"mint": "X", "reason": reason,
                                   "closed_at": (now - timedelta(minutes=5)).isoformat()}]
                self.assertFalse(p.recently_stopped_out(closed_trades, "X", now, cooldown_hours=4.0))

    def test_malformed_closed_at_degrades_to_false_not_a_crash(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        closed_trades = [{"mint": "X", "reason": "stop-loss (-15.0%)", "closed_at": "not-a-timestamp"}]
        self.assertFalse(p.recently_stopped_out(closed_trades, "X", now, cooldown_hours=4.0))

    def test_empty_closed_trades(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(p.recently_stopped_out([], "X", now, cooldown_hours=4.0))


class TestMemecoinExposureUsd(unittest.TestCase):
    """memecoin_exposure_usd() sums scout+emerging tier value -- what
    max_memecoin_exposure_fraction caps ('risk a maximum of 5% of total
    portfolio on memecoins')."""

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
    """scout_disco_args() loosens maturity/liquidity/distribution thresholds
    for very-new tokens, but must NOT loosen the hard anti-rug checks --
    those (not age or liquidity) are what actually prevent a malicious
    mint from draining a pool."""

    def _base_args(self):
        return argparse.Namespace(
            request_delay=0.4,
            min_liquidity_usd=250_000, min_holder_count=500, min_organic_score=40, min_pool_age_hours=72,
            max_top_holder_pct=22.0,
            blue_chip_mcap_usd=50_000_000, blue_chip_holder_count=10_000,
            established_mcap_usd=5_000_000, established_holder_count=2_000,
            scout_min_liquidity_usd=20_000, scout_min_holder_count=30, scout_min_organic_score=20,
            scout_min_pool_age_hours=1, scout_max_top_holder_pct=35.0,
        )

    def test_thresholds_are_looser_than_normal(self):
        scout = p.scout_disco_args(self._base_args())
        self.assertLess(scout.min_liquidity_usd, 250_000)
        self.assertLess(scout.min_holder_count, 500)
        self.assertLess(scout.min_pool_age_hours, 72)
        self.assertGreater(scout.max_top_holder_pct, 22.0)

    def test_hard_anti_rug_checks_are_unchanged(self):
        scout = p.scout_disco_args(self._base_args())
        self.assertTrue(scout.require_mint_renounced)
        self.assertTrue(scout.require_freeze_renounced)


class TestSizePosition(unittest.TestCase):
    def test_no_history_uses_minimum_size_only(self):
        # trade-cycle's documented rule: no vol-scaling for a token with no
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


class TestSelectCandidatesForRotation(unittest.TestCase):
    """select_candidates_for_rotation() ensures discovery's max_candidates cap
    rotates across the full discovered set over multiple cycles, instead of
    always evaluating the same head-of-list tokens -- verified live that a
    real cycle found 63 unique candidates against a cap of 40, permanently
    starving the last 23 under the old fixed-order truncation."""

    def test_unseen_candidates_prioritized_over_recently_evaluated(self):
        all_mints = ["A", "B", "C", "D"]
        recently_evaluated = {"A", "B"}
        result = p.select_candidates_for_rotation(all_mints, max_candidates=2, recently_evaluated=recently_evaluated)
        self.assertEqual(set(result), {"C", "D"})

    def test_falls_back_to_recently_evaluated_when_not_enough_unseen(self):
        all_mints = ["A", "B", "C"]
        recently_evaluated = {"A", "B"}
        result = p.select_candidates_for_rotation(all_mints, max_candidates=3, recently_evaluated=recently_evaluated)
        self.assertEqual(set(result), {"A", "B", "C"})

    def test_held_position_always_included_even_if_recently_evaluated(self):
        all_mints = ["HELD", "B", "C", "D", "E"]
        recently_evaluated = {"HELD"}  # was bought before, so it's "recently evaluated" -- must not be excluded
        result = p.select_candidates_for_rotation(all_mints, max_candidates=2, recently_evaluated=recently_evaluated,
                                                    held_mints={"HELD"})
        self.assertIn("HELD", result)

    def test_held_position_does_not_count_against_the_cap(self):
        all_mints = ["HELD", "B", "C", "D"]
        result = p.select_candidates_for_rotation(all_mints, max_candidates=2, recently_evaluated=set(),
                                                    held_mints={"HELD"})
        # HELD plus a full 2 slots of non-held candidates
        self.assertEqual(len(result), 3)
        self.assertIn("HELD", result)

    def test_no_rotation_needed_when_everything_fits_under_the_cap(self):
        all_mints = ["A", "B"]
        result = p.select_candidates_for_rotation(all_mints, max_candidates=10, recently_evaluated=set())
        self.assertEqual(set(result), {"A", "B"})

    def test_selection_is_shuffled_not_biased_toward_the_front_of_all_mints(self):
        """Real bug this guards against: gather_candidates() lists momentum
        sources (organic/trending/traded/recent, ~70-130 tokens) before the
        verified-tag source (~2,561 tokens) -- without shuffling, the small,
        fast-cycling momentum pool alone always supplied enough "unseen"
        tokens to fill every slot, so selection never reached past roughly
        index 130 of a 2,596-token pool, cycle after cycle. Verified live: 43
        selected candidates in a real cycle, all from position 0-126.
        Simulates that shape -- a small "front" block plus a huge "tail" --
        and asserts the tail actually gets picked sometimes, not never."""
        front = [f"front{i}" for i in range(100)]
        tail = [f"tail{i}" for i in range(2500)]
        all_mints = front + tail
        selected_from_tail_ever = False
        for _ in range(20):
            result = p.select_candidates_for_rotation(all_mints, max_candidates=40, recently_evaluated=set())
            if any(m.startswith("tail") for m in result):
                selected_from_tail_ever = True
                break
        self.assertTrue(selected_from_tail_ever,
                         "tail (the large, previously-starved pool) was never selected across 20 cycles")


class TestDiscoveryRotationPersistence(unittest.TestCase):
    def setUp(self):
        self._orig_path = p.DISCOVERY_ROTATION_PATH
        p.DISCOVERY_ROTATION_PATH = Path(__file__).resolve().parent / "_tmp_discovery_rotation_test.json"
        if p.DISCOVERY_ROTATION_PATH.exists():
            p.DISCOVERY_ROTATION_PATH.unlink()

    def tearDown(self):
        if p.DISCOVERY_ROTATION_PATH.exists():
            p.DISCOVERY_ROTATION_PATH.unlink()
        p.DISCOVERY_ROTATION_PATH = self._orig_path

    def test_empty_when_no_file_exists(self):
        self.assertEqual(p._load_recently_evaluated(), set())

    def test_round_trip_through_save_and_load(self):
        p._save_recently_evaluated(["A", "B", "C"], max_candidates=10)
        self.assertEqual(p._load_recently_evaluated(), {"A", "B", "C"})

    def test_history_window_is_bounded_and_old_entries_age_out(self):
        # max_candidates=2, history=DISCOVERY_ROTATION_HISTORY_CYCLES(3) -> keeps last 6 entries
        p._save_recently_evaluated(["A", "B"], max_candidates=2)
        p._save_recently_evaluated(["C", "D"], max_candidates=2)
        p._save_recently_evaluated(["E", "F"], max_candidates=2)
        p._save_recently_evaluated(["G", "H"], max_candidates=2)
        recent = p._load_recently_evaluated()
        self.assertNotIn("A", recent, "oldest cycle's mints should have aged out of the bounded window")
        self.assertIn("G", recent)
        self.assertIn("H", recent)


class TestRegimeCache(unittest.TestCase):
    """regime_allows_new_entries() caches its result for
    REGIME_CACHE_TTL_SECONDS instead of hitting CoinGecko every paper cycle
    -- a 30-day SMA doesn't meaningfully change in 15 minutes, and this was a
    real fix for CoinGecko 429s that were adding ~60s of backoff to nearly
    every cycle."""

    def setUp(self):
        self._orig_path = p.REGIME_CACHE_PATH
        p.REGIME_CACHE_PATH = Path(__file__).resolve().parent / "_tmp_regime_cache_test.json"
        if p.REGIME_CACHE_PATH.exists():
            p.REGIME_CACHE_PATH.unlink()

    def tearDown(self):
        if p.REGIME_CACHE_PATH.exists():
            p.REGIME_CACHE_PATH.unlink()
        p.REGIME_CACHE_PATH = self._orig_path

    @patch("backtest.fetch_history.fetch_market_chart")
    def test_fetches_live_on_empty_cache_then_caches_it(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(40)]}
        result1 = p.regime_allows_new_entries("bitcoin", 30)
        result2 = p.regime_allows_new_entries("bitcoin", 30)
        self.assertEqual(mock_fetch.call_count, 1, "second call within TTL should use the cache, not refetch")
        self.assertEqual(result1, result2)

    @patch("backtest.fetch_history.fetch_market_chart")
    def test_different_reference_coin_bypasses_stale_cache_entry(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(40)]}
        p.regime_allows_new_entries("bitcoin", 30)
        p.regime_allows_new_entries("ethereum", 30)
        self.assertEqual(mock_fetch.call_count, 2, "a different coin must not reuse another coin's cached read")

    @patch("backtest.fetch_history.fetch_market_chart")
    def test_expired_cache_entry_triggers_a_refetch(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(40)]}
        p.regime_allows_new_entries("bitcoin", 30)
        # backdate the cache past the TTL
        cached = json.loads(p.REGIME_CACHE_PATH.read_text())
        stale = datetime.now(timezone.utc) - timedelta(seconds=p.REGIME_CACHE_TTL_SECONDS + 1)
        cached["computed_at"] = stale.isoformat()
        p.REGIME_CACHE_PATH.write_text(json.dumps(cached))
        p.regime_allows_new_entries("bitcoin", 30)
        self.assertEqual(mock_fetch.call_count, 2)

    def test_malformed_but_valid_json_cache_degrades_to_none_not_a_crash(self):
        """Real bug found live: a cache file that's valid JSON but missing
        computed_at (partial write, disk issue, manual edit) raised an
        uncaught KeyError from datetime.fromisoformat(cached["computed_at"]),
        crashing the whole cycle instead of falling back to a live fetch --
        the same class of bug already fixed once this session in
        _pool_age_hours()."""
        p.REGIME_CACHE_PATH.write_text(json.dumps({
            "reference_coin": "bitcoin", "sma_window_days": 30, "allows_new_entries": True,
        }))  # missing computed_at
        self.assertIsNone(p._load_regime_cache("bitcoin", 30))

    def test_non_iso_computed_at_degrades_to_none_not_a_crash(self):
        p.REGIME_CACHE_PATH.write_text(json.dumps({
            "reference_coin": "bitcoin", "sma_window_days": 30, "allows_new_entries": True,
            "computed_at": "not-a-real-timestamp",
        }))
        self.assertIsNone(p._load_regime_cache("bitcoin", 30))


class TestPriceHistoryCache(unittest.TestCase):
    """get_price_history_closes() caches per (mint, days) for
    PRICE_HISTORY_CACHE_TTL_SECONDS -- with several eligible candidates per
    cycle, this was the dominant source of CoinGecko 429 backoff (one
    uncached fetch per candidate, every 15-minute cycle)."""

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

    @patch("backtest.fetch_history.fetch_market_chart_by_contract")
    def test_second_call_within_ttl_skips_the_network(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(20)]}
        closes1 = p.get_price_history_closes("MintA", 90)
        closes2 = p.get_price_history_closes("MintA", 90)
        self.assertEqual(mock_fetch.call_count, 1)
        self.assertEqual(closes1, closes2)

    @patch("backtest.fetch_history.fetch_market_chart_by_contract")
    def test_uses_an_impatient_retry_policy_not_the_backtest_default(self, mock_fetch):
        """This runs inside a tight 15-minute cron loop, not a one-off
        backtest -- fetch_history.py's default retry policy (~100s worst
        case) repeatedly cost whole cycles 1m40s+ once several candidates
        each hit it. A candidate this gives up on quickly just gets
        reconsidered next cycle, so failing fast here is the right trade."""
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(20)]}
        p.get_price_history_closes("MintA", 90)
        _, kwargs = mock_fetch.call_args
        self.assertLessEqual(kwargs.get("retries", 4), 2)
        self.assertLessEqual(kwargs.get("base_wait", 10.0), 3.0)

    @patch("backtest.fetch_history.fetch_market_chart_by_contract")
    def test_different_mint_is_not_served_from_another_mints_cache(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(20)]}
        p.get_price_history_closes("MintA", 90)
        p.get_price_history_closes("MintB", 90)
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("backtest.fetch_history.fetch_market_chart_by_contract")
    def test_expired_cache_entry_triggers_a_refetch(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(20)]}
        p.get_price_history_closes("MintA", 90)
        cache_path = p._price_history_cache_path("MintA", 90)
        cached = json.loads(cache_path.read_text())
        stale = datetime.now(timezone.utc) - timedelta(seconds=p.PRICE_HISTORY_CACHE_TTL_SECONDS + 1)
        cached["computed_at"] = stale.isoformat()
        cache_path.write_text(json.dumps(cached))
        p.get_price_history_closes("MintA", 90)
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("backtest.fetch_history.fetch_market_chart_by_contract")
    def test_insufficient_history_is_not_cached(self, mock_fetch):
        mock_fetch.return_value = {"prices": [[i, 100.0 + i] for i in range(5)]}  # under the 15-point minimum
        result = p.get_price_history_closes("MintA", 90)
        self.assertIsNone(result)

    def test_malformed_but_valid_json_cache_degrades_to_none_not_a_crash(self):
        """Same real bug as TestRegimeCache's equivalent test, in the sibling
        cache function."""
        p._price_history_cache_path("MintA", 90).parent.mkdir(parents=True, exist_ok=True)
        p._price_history_cache_path("MintA", 90).write_text(json.dumps({"closes": [1.0, 2.0]}))  # missing computed_at
        self.assertIsNone(p._load_price_history_cache("MintA", 90))

    @patch("backtest.fetch_history.fetch_market_chart_by_contract")
    def test_caller_supplied_cached_value_skips_the_internal_lookup(self, mock_fetch):
        """get_price_history_closes(mint, days, cached=...) lets a caller that
        already did its own _load_price_history_cache() lookup (e.g. to
        decide whether this fetch counts against a per-cycle budget) pass the
        result straight through instead of the function re-reading and
        re-parsing the same cache file a second time."""
        with patch.object(p, "_load_price_history_cache") as mock_load:
            result = p.get_price_history_closes("MintA", 90, cached=[1.0, 2.0, 3.0])
            mock_load.assert_not_called()
        self.assertEqual(result, [1.0, 2.0, 3.0])
        mock_fetch.assert_not_called()
        self.assertFalse(p._price_history_cache_path("MintA", 90).exists())


class TestCurrentPrices(unittest.TestCase):
    """current_prices() batches N mints into as few Jupiter /search calls as
    possible instead of one call per mint -- this was a real fix (see
    CHUNK_SIZE) for 429s that were firing on nearly every paper cycle from a
    one-request-per-mint loop against a free, rate-limited endpoint."""

    @patch("research.discover_candidates._get_json")
    def test_single_batched_call_for_mints_under_chunk_size(self, mock_get):
        mock_get.return_value = [
            {"id": "MintA", "symbol": "AAA", "usdPrice": 1.5},
            {"id": "MintB", "symbol": "BBB", "usdPrice": 2.5},
        ]
        prices = p.current_prices(["MintA", "MintB"])
        self.assertEqual(mock_get.call_count, 1, "should be one batched call, not one per mint")
        self.assertEqual(prices, {"MintA": 1.5, "MintB": 2.5})

    @patch("research.discover_candidates._get_json")
    def test_chunks_when_more_mints_than_chunk_size(self, mock_get):
        mock_get.return_value = []
        mints = [f"Mint{i}" for i in range(p.CHUNK_SIZE + 5)]
        p.current_prices(mints)
        expected_chunks = -(-len(mints) // p.CHUNK_SIZE)  # ceil division
        self.assertEqual(mock_get.call_count, expected_chunks)

    @patch("research.discover_candidates._get_json")
    def test_mint_absent_from_response_is_simply_missing(self, mock_get):
        mock_get.return_value = [{"id": "MintA", "symbol": "AAA", "usdPrice": 1.5}]
        prices = p.current_prices(["MintA", "MintB"])
        self.assertEqual(prices, {"MintA": 1.5})
        self.assertNotIn("MintB", prices)

    @patch("research.discover_candidates._get_json")
    def test_duplicate_mints_deduped_before_querying(self, mock_get):
        mock_get.return_value = [{"id": "MintA", "symbol": "AAA", "usdPrice": 1.5}]
        p.current_prices(["MintA", "MintA", "MintA"])
        called_url = mock_get.call_args[0][0]
        self.assertEqual(called_url.count("MintA"), 1)

    @patch("research.discover_candidates._get_json")
    def test_single_mint_wrapper_still_works(self, mock_get):
        mock_get.return_value = [{"id": "MintA", "symbol": "AAA", "usdPrice": 1.5}]
        self.assertEqual(p.current_price("MintA"), 1.5)


if __name__ == "__main__":
    unittest.main()
