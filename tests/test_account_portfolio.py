"""Unit tests for account/portfolio.py -- synthetic balance/price dicts, no
network calls, no subprocess. Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from account import portfolio as p  # noqa: E402


class TestNormalizeBalances(unittest.TestCase):
    def test_strips_legacy_prefixes(self):
        raw = {"ZUSD": "120.50", "XXBT": "0.01", "XETH": "0.5", "SOL": "3.0"}
        result = p.normalize_balances(raw)
        self.assertEqual(result, {"USD": 120.50, "BTC": 0.01, "ETH": 0.5, "SOL": 3.0})

    def test_drops_dust_below_epsilon(self):
        raw = {"ZUSD": "100.0", "XXBT": "1e-12"}
        result = p.normalize_balances(raw)
        self.assertEqual(result, {"USD": 100.0})

    def test_ignores_unparseable_amounts_rather_than_raising(self):
        raw = {"ZUSD": "not-a-number", "SOL": "2.0"}
        result = p.normalize_balances(raw)
        self.assertEqual(result, {"SOL": 2.0})

    def test_merges_duplicate_normalized_codes(self):
        # defensive: if a response ever mixed legacy and already-normalized
        # codes for the same asset, they should sum, not silently overwrite
        raw = {"XXBT": "0.01", "BTC": "0.02"}
        result = p.normalize_balances(raw)
        self.assertAlmostEqual(result["BTC"], 0.03)

    def test_empty_input_returns_empty(self):
        self.assertEqual(p.normalize_balances({}), {})
        self.assertEqual(p.normalize_balances(None), {})


class TestUsdValueOfBalances(unittest.TestCase):
    def test_stable_assets_default_to_one_dollar(self):
        v = p.usd_value_of_balances({"USD": 100.0, "USDT": 50.0})
        self.assertEqual(v.total_usd, 150.0)
        self.assertEqual(v.cash_usd, 150.0)
        self.assertEqual(v.non_cash_usd, 0.0)
        self.assertEqual(v.pricing_errors, [])

    def test_stable_asset_price_override_reflects_depeg(self):
        v = p.usd_value_of_balances({"USDT": 100.0}, prices_usd={"USDT": 0.995})
        self.assertAlmostEqual(v.total_usd, 99.5)

    def test_non_stable_asset_priced_from_ticker(self):
        v = p.usd_value_of_balances({"BTC": 0.5}, prices_usd={"BTC": 60000.0})
        self.assertAlmostEqual(v.total_usd, 30000.0)
        self.assertAlmostEqual(v.non_cash_usd, 30000.0)
        self.assertEqual(v.cash_usd, 0.0)

    def test_missing_price_excludes_from_total_and_flags_error(self):
        v = p.usd_value_of_balances({"USD": 100.0, "SOME_UNPRICED": 5.0})
        self.assertEqual(v.total_usd, 100.0)   # NOT 100.0 + garbage -- the unpriced asset contributes 0, not "excluded from the dict"
        self.assertEqual(len(v.pricing_errors), 1)
        self.assertIn("SOME_UNPRICED", v.pricing_errors[0])
        unpriced = [a for a in v.assets if a.asset == "SOME_UNPRICED"][0]
        self.assertIsNone(unpriced.price_usd)
        self.assertEqual(unpriced.value_usd, 0.0)

    def test_missing_price_biases_conservative_not_optimistic(self):
        # a portfolio genuinely worth far more than total_usd reports (because
        # one holding couldn't be priced) must never be reported as MORE than
        # its priceable total -- the bias must only ever be downward.
        v = p.usd_value_of_balances({"USD": 40.0, "UNPRICEABLE": 1000.0})
        self.assertEqual(v.total_usd, 40.0)

    def test_non_stable_exposure_fraction(self):
        v = p.usd_value_of_balances({"USD": 50.0, "BTC": 1.0}, prices_usd={"BTC": 50.0})
        self.assertAlmostEqual(v.non_stable_exposure_fraction, 0.5)

    def test_non_stable_exposure_fraction_zero_when_portfolio_empty(self):
        v = p.usd_value_of_balances({})
        self.assertEqual(v.non_stable_exposure_fraction, 0.0)

    def test_to_dict_round_trips_key_fields(self):
        v = p.usd_value_of_balances({"USD": 10.0, "BTC": 1.0}, prices_usd={"BTC": 100.0})
        d = v.to_dict()
        self.assertEqual(d["total_usd"], 110.0)
        self.assertEqual(len(d["assets"]), 2)
        self.assertIn("non_stable_exposure_fraction", d)


if __name__ == "__main__":
    unittest.main()
