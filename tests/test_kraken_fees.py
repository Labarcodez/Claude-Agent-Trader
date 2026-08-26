"""Unit tests for kraken/fees.py -- pure math + response parsing, no
network calls. Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kraken import fees  # noqa: E402


def trade_volume_response(pair="XBTUSD", taker_fee="0.2600", maker_fee="0.1600",
                           volume="15000.0000", nextfee="0.2400", nextvolume="250000.0000"):
    return {
        "error": [],
        "result": {
            "currency": "ZUSD", "volume": volume,
            "fees": {pair: {"fee": taker_fee, "nextfee": nextfee, "nextvolume": nextvolume}},
            "fees_maker": {pair: {"fee": maker_fee}},
        },
    }


class TestParseFeeTier(unittest.TestCase):
    def test_parses_real_tier_from_response(self):
        tier = fees.parse_fee_tier(trade_volume_response(), "XBTUSD", 40, 25)
        self.assertAlmostEqual(tier.taker_fee_bps, 26.0)
        self.assertAlmostEqual(tier.maker_fee_bps, 16.0)
        self.assertFalse(tier.used_default)
        self.assertAlmostEqual(tier.volume_30d_usd, 15000.0)
        self.assertAlmostEqual(tier.next_tier_taker_fee_bps, 24.0)

    def test_falls_back_to_defaults_when_pair_not_in_response(self):
        tier = fees.parse_fee_tier(trade_volume_response(pair="ETHUSD"), "XBTUSD", 40, 25)
        self.assertEqual(tier.taker_fee_bps, 40)
        self.assertEqual(tier.maker_fee_bps, 25)
        self.assertTrue(tier.used_default)

    def test_handles_result_not_nested_under_result_key(self):
        # some callers may pass the already-unwrapped "result" dict directly
        raw = trade_volume_response()["result"]
        tier = fees.parse_fee_tier(raw, "XBTUSD", 40, 25)
        self.assertAlmostEqual(tier.taker_fee_bps, 26.0)

    def test_malformed_volume_degrades_to_none_not_a_crash(self):
        resp = trade_volume_response(volume="not-a-number")
        tier = fees.parse_fee_tier(resp, "XBTUSD", 40, 25)
        self.assertIsNone(tier.volume_30d_usd)


class TestRoundTripCostUsd(unittest.TestCase):
    def test_basic_cost_calculation(self):
        # $100 position, 26bps entry + 26bps exit = 52bps = $0.52
        cost = fees.round_trip_cost_usd(100.0, entry_fee_bps=26, exit_fee_bps=26)
        self.assertAlmostEqual(cost, 0.52)

    def test_includes_entry_spread_when_given(self):
        cost_no_spread = fees.round_trip_cost_usd(100.0, 26, 26, entry_spread_bps=0)
        cost_with_spread = fees.round_trip_cost_usd(100.0, 26, 26, entry_spread_bps=10)
        self.assertGreater(cost_with_spread, cost_no_spread)


class TestCompareMakerVsTaker(unittest.TestCase):
    def test_taker_costs_more_when_spread_present(self):
        result = fees.compare_maker_vs_taker(100.0, maker_fee_bps=16, taker_fee_bps=26, spread_bps=10)
        self.assertGreater(result["taker_round_trip_cost_usd"], result["maker_round_trip_cost_usd"])
        self.assertGreater(result["savings_usd_from_preferring_maker"], 0)


class TestEdgeClearsCosts(unittest.TestCase):
    def test_clears_when_profit_comfortably_exceeds_costs(self):
        result = fees.edge_clears_costs(size_usd=1000.0, take_profit_pct=0.35,
                                         entry_fee_bps=16, exit_fee_bps=26, min_edge_multiple=2.0)
        self.assertTrue(result["clears"])
        self.assertGreater(result["edge_to_cost_multiple"], 2.0)

    def test_does_not_clear_for_a_tiny_take_profit_target(self):
        result = fees.edge_clears_costs(size_usd=1000.0, take_profit_pct=0.001,
                                         entry_fee_bps=16, exit_fee_bps=26, min_edge_multiple=2.0)
        self.assertFalse(result["clears"])

    def test_zero_cost_is_treated_as_infinite_multiple(self):
        result = fees.edge_clears_costs(size_usd=1000.0, take_profit_pct=0.35,
                                         entry_fee_bps=0, exit_fee_bps=0, min_edge_multiple=2.0)
        self.assertTrue(result["clears"])
        self.assertEqual(result["edge_to_cost_multiple"], float("inf"))


if __name__ == "__main__":
    unittest.main()
