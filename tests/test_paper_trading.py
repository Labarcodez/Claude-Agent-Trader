"""Unit tests for paper_trading/run_paper_cycle.py's pure sizing/risk logic
-- synthetic state and price dicts, no network calls, no state file I/O.
Run: python3 -m unittest discover -s tests -v"""
import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paper_trading import run_paper_cycle as p  # noqa: E402


def make_size_args(**overrides):
    defaults = dict(
        max_position_usd=200.0,
        max_position_fraction=0.30,
        min_trade_usd=10.0,
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
        state = {"cash_usd": 500.0, "positions": {}}
        self.assertEqual(p.portfolio_value_usd(state, {}), 500.0)

    def test_cash_plus_positions(self):
        state = {"cash_usd": 100.0, "positions": {"XBTUSD": {"quantity": 2.0}, "ETHUSD": {"quantity": 5.0}}}
        prices = {"XBTUSD": 3.0, "ETHUSD": 1.0}
        self.assertAlmostEqual(p.portfolio_value_usd(state, prices), 100.0 + 2.0 * 3.0 + 5.0 * 1.0)

    def test_missing_price_excludes_position_rather_than_erroring(self):
        state = {"cash_usd": 10.0, "positions": {"XBTUSD": {"quantity": 2.0}}}
        self.assertEqual(p.portfolio_value_usd(state, {}), 10.0)


class TestPortfolioHeatPct(unittest.TestCase):
    def test_zero_with_no_positions(self):
        state = {"cash_usd": 500.0, "positions": {}}
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
        blue_chip = p.size_position("blue_chip", 1000.0, flat, args)
        established = p.size_position("established", 1000.0, flat, args)
        emerging = p.size_position("emerging", 1000.0, flat, args)
        self.assertGreater(blue_chip, established)
        self.assertGreater(established, emerging)

    def test_higher_volatility_shrinks_size(self):
        args = make_size_args()
        calm = [100.0, 100.1, 99.9, 100.2, 99.8] * 5
        wild = [100.0, 130.0, 70.0, 140.0, 60.0] * 5
        calm_size = p.size_position("blue_chip", 1000.0, calm, args)
        wild_size = p.size_position("blue_chip", 1000.0, wild, args)
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


class TestCurrentPricePrefersFirstTickerResult(unittest.TestCase):
    def test_parses_last_trade_price_from_kraken_ticker_shape(self):
        from unittest.mock import patch
        fake_result = {"XXBTZUSD": {"c": ["65000.5", "0.01"]}}
        with patch("research.discover_candidates._get_json", return_value=fake_result):
            self.assertEqual(p.current_price("XBTUSD"), 65000.5)

    def test_none_when_no_result(self):
        from unittest.mock import patch
        with patch("research.discover_candidates._get_json", return_value=None):
            self.assertIsNone(p.current_price("XBTUSD"))


if __name__ == "__main__":
    unittest.main()
