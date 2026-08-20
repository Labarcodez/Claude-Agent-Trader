"""Unit tests for paper_trading/run_paper_cycle.py's pure sizing/risk logic
-- synthetic state and price dicts, no network calls, no state file I/O.
Run: python3 -m unittest discover -s tests -v"""
import argparse
import json
import sys
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
        # config/discovery.yaml's tiers block is the source of truth for these
        # numbers; this test exists so a change to one doesn't silently drift
        # from the other.
        self.assertEqual(p.TIER_MULTIPLIERS, {"blue_chip": 1.0, "established": 0.7, "emerging": 0.4})


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
