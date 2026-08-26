"""Unit tests for account/precision.py -- pure arithmetic, no network calls.
Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from account import precision  # noqa: E402


class TestRoundVolume(unittest.TestCase):
    def test_floors_excess_decimals(self):
        self.assertAlmostEqual(precision.round_volume(0.123456789, 4), 0.1234)

    def test_never_rounds_up(self):
        # 0.99995 at 4 decimals must floor to 0.9999, not round to 1.0000
        # (rounding up could claim more volume than was actually affordable)
        self.assertAlmostEqual(precision.round_volume(0.99995, 4), 0.9999)

    def test_none_lot_decimals_returns_unchanged(self):
        self.assertEqual(precision.round_volume(1.23456789, None), 1.23456789)

    def test_zero_or_negative_volume_returned_unchanged(self):
        self.assertEqual(precision.round_volume(0.0, 4), 0.0)
        self.assertEqual(precision.round_volume(-1.0, 4), -1.0)

    def test_already_precise_value_unchanged(self):
        self.assertAlmostEqual(precision.round_volume(1.5, 2), 1.5)


class TestRoundPrice(unittest.TestCase):
    def test_rounds_to_nearest_not_floor(self):
        # unlike volume, price rounds to nearest -- 50000.567 at 1 decimal
        # should round UP to 50000.6, not floor to 50000.5
        self.assertAlmostEqual(precision.round_price(50000.567, 1), 50000.6)

    def test_none_pair_decimals_returns_unchanged(self):
        self.assertEqual(precision.round_price(1.23456, None), 1.23456)

    def test_integer_pair_decimals_of_zero(self):
        self.assertEqual(precision.round_price(50000.6, 0), 50001.0)


class TestClampToPairMinimums(unittest.TestCase):
    def test_size_above_both_minimums_is_ok(self):
        result = precision.clamp_to_pair_minimums(size_usd=100.0, price=50000.0, ordermin=0.0001, costmin=0.5)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])
        # ordermin*price = $5, costmin = $0.5 -> floor is $5
        self.assertAlmostEqual(result["min_required_usd"], 5.0)

    def test_size_below_costmin_rejected(self):
        result = precision.clamp_to_pair_minimums(size_usd=1.0, price=50000.0, ordermin=None, costmin=5.0)
        self.assertFalse(result["ok"])
        self.assertIn("$1.00", result["reason"])
        self.assertAlmostEqual(result["min_required_usd"], 5.0)

    def test_size_below_ordermin_times_price_rejected(self):
        # ordermin of 1 unit at a $10 price means the floor is $10, well
        # above a tiny costmin
        result = precision.clamp_to_pair_minimums(size_usd=5.0, price=10.0, ordermin=1.0, costmin=0.5)
        self.assertFalse(result["ok"])
        self.assertAlmostEqual(result["min_required_usd"], 10.0)

    def test_missing_price_is_rejected_not_crashed(self):
        result = precision.clamp_to_pair_minimums(size_usd=100.0, price=None, ordermin=1.0, costmin=1.0)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["min_required_usd"])

    def test_zero_price_is_rejected_not_divide_by_zero(self):
        result = precision.clamp_to_pair_minimums(size_usd=100.0, price=0.0, ordermin=1.0, costmin=1.0)
        self.assertFalse(result["ok"])

    def test_missing_ordermin_and_costmin_defaults_to_no_floor(self):
        result = precision.clamp_to_pair_minimums(size_usd=1.0, price=100.0, ordermin=None, costmin=None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["min_required_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
