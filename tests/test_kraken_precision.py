"""Unit tests for kraken/precision.py -- pure math, no network calls.
Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kraken import precision as kp  # noqa: E402


class TestRoundVolume(unittest.TestCase):
    def test_floors_to_lot_decimals(self):
        self.assertAlmostEqual(kp.round_volume(1.23456789, 4), 1.2345)

    def test_never_rounds_up(self):
        # 1.99999 at 2 decimals must floor to 1.99, not round to 2.00 --
        # rounding up could claim more volume than was actually affordable
        self.assertAlmostEqual(kp.round_volume(1.99999, 2), 1.99)

    def test_none_lot_decimals_returns_volume_unchanged(self):
        self.assertEqual(kp.round_volume(1.23456789, None), 1.23456789)

    def test_zero_or_negative_volume_returned_unchanged(self):
        self.assertEqual(kp.round_volume(0.0, 4), 0.0)
        self.assertEqual(kp.round_volume(-1.0, 4), -1.0)

    def test_binary_float_representation_noise_does_not_truncate_an_extra_tick(self):
        # 0.29 * 100 == 28.999999999999996 in IEEE 754 double precision --
        # a bare floor() truncates this to 0.28, silently losing a full
        # tick of a genuinely-intended 0.29. Real bug, found by code review.
        self.assertAlmostEqual(kp.round_volume(0.29, 2), 0.29, places=8)
        self.assertAlmostEqual(kp.round_volume(2.005, 2), 2.00, places=8)


class TestRoundPrice(unittest.TestCase):
    def test_rounds_to_nearest_not_floored(self):
        # 100.017 unambiguously rounds up to 100.02 at 2 decimals (unlike an
        # exact .005 boundary, which float representation makes unreliable
        # to assert on either direction)
        self.assertAlmostEqual(kp.round_price(100.017, 2), 100.02, places=2)

    def test_none_pair_decimals_returns_price_unchanged(self):
        self.assertEqual(kp.round_price(100.123456, None), 100.123456)


class TestClampToPairMinimums(unittest.TestCase):
    def test_ok_when_size_clears_both_floors(self):
        result = kp.clamp_to_pair_minimums(size_usd=50.0, price=100.0, ordermin=0.1, costmin=5.0)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])
        self.assertAlmostEqual(result["min_required_usd"], 10.0)  # ordermin*price=10 > costmin=5

    def test_rejects_below_costmin(self):
        result = kp.clamp_to_pair_minimums(size_usd=2.0, price=100.0, ordermin=0.01, costmin=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("pair minimum", result["reason"])

    def test_ordermin_times_price_can_exceed_costmin(self):
        # an expensive base asset (e.g. BTC) can push the effective floor
        # above costmin via ordermin*price alone
        result = kp.clamp_to_pair_minimums(size_usd=8.0, price=50_000.0, ordermin=0.0002, costmin=1.0)
        self.assertAlmostEqual(result["min_required_usd"], 10.0)
        self.assertFalse(result["ok"])

    def test_non_positive_price_is_rejected(self):
        result = kp.clamp_to_pair_minimums(size_usd=50.0, price=0.0, ordermin=0.1, costmin=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("price", result["reason"])

    def test_missing_ordermin_and_costmin_defaults_to_zero_floor(self):
        result = kp.clamp_to_pair_minimums(size_usd=0.01, price=100.0, ordermin=None, costmin=None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["min_required_usd"], 0.0)

    def test_malformed_ordermin_does_not_crash(self):
        result = kp.clamp_to_pair_minimums(size_usd=50.0, price=100.0, ordermin="not-a-number", costmin=5.0)
        self.assertTrue(result["ok"])  # falls back to costmin=5.0 only


if __name__ == "__main__":
    unittest.main()
