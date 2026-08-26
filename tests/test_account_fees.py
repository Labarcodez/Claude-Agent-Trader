"""Unit tests for account/fees.py -- synthetic Kraken TradeVolume-shaped
dicts, no network calls, no subprocess. Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from account import fees  # noqa: E402


def trade_volume_result(pair="XBTUSD", taker_fee="0.2600", maker_fee="0.1600",
                         volume="1000.0000", nextfee="0.2400", nextvolume="250000.0000"):
    return {
        "error": [],
        "result": {
            "currency": "ZUSD",
            "volume": volume,
            "fees": {pair: {"fee": taker_fee, "minfee": "0.1000", "maxfee": "0.4000",
                             "nextfee": nextfee, "nextvolume": nextvolume, "tiervolume": "0.0000"}},
            "fees_maker": {pair: {"fee": maker_fee, "minfee": "0.0000", "maxfee": "0.2500",
                                   "nextfee": "0.1400", "nextvolume": nextvolume, "tiervolume": "0.0000"}},
        },
    }


class TestParseFeeTier(unittest.TestCase):
    def test_parses_percent_strings_to_bps(self):
        tier = fees.parse_fee_tier(trade_volume_result(), "XBTUSD", 40.0, 25.0)
        self.assertAlmostEqual(tier.taker_fee_bps, 26.0)
        self.assertAlmostEqual(tier.maker_fee_bps, 16.0)
        self.assertFalse(tier.used_default)

    def test_parses_volume_and_next_tier_fields(self):
        tier = fees.parse_fee_tier(trade_volume_result(), "XBTUSD", 40.0, 25.0)
        self.assertAlmostEqual(tier.volume_30d_usd, 1000.0)
        self.assertAlmostEqual(tier.next_tier_volume_usd, 250000.0)
        self.assertAlmostEqual(tier.next_tier_taker_fee_bps, 24.0)

    def test_falls_back_to_defaults_when_pair_not_present(self):
        result = trade_volume_result(pair="ETHUSD")   # response only has fees for ETHUSD
        tier = fees.parse_fee_tier(result, "XBTUSD", 40.0, 25.0)   # but we ask about XBTUSD
        self.assertEqual(tier.taker_fee_bps, 40.0)
        self.assertEqual(tier.maker_fee_bps, 25.0)
        self.assertTrue(tier.used_default)

    def test_handles_unwrapped_result_shape(self):
        # some callers might already have unwrapped the "result" key
        wrapped = trade_volume_result()
        tier = fees.parse_fee_tier(wrapped["result"], "XBTUSD", 40.0, 25.0)
        self.assertAlmostEqual(tier.taker_fee_bps, 26.0)

    def test_malformed_volume_does_not_raise(self):
        result = trade_volume_result(volume="not-a-number")
        tier = fees.parse_fee_tier(result, "XBTUSD", 40.0, 25.0)
        self.assertIsNone(tier.volume_30d_usd)


class TestRoundTripCost(unittest.TestCase):
    def test_basic_round_trip_cost(self):
        # $1000 position, 26bps in + 26bps out = 52bps = $5.20
        cost = fees.round_trip_cost_usd(1000.0, entry_fee_bps=26, exit_fee_bps=26)
        self.assertAlmostEqual(cost, 5.20)

    def test_spread_adds_to_cost(self):
        no_spread = fees.round_trip_cost_usd(1000.0, 26, 26, entry_spread_bps=0)
        with_spread = fees.round_trip_cost_usd(1000.0, 26, 26, entry_spread_bps=10)
        self.assertGreater(with_spread, no_spread)
        self.assertAlmostEqual(with_spread - no_spread, 1.0)   # 10bps of $1000 = $1

    def test_zero_size_zero_cost(self):
        self.assertEqual(fees.round_trip_cost_usd(0.0, 26, 26), 0.0)


class TestCompareMakerVsTaker(unittest.TestCase):
    def test_maker_cheaper_than_taker_when_maker_fee_lower(self):
        cmp = fees.compare_maker_vs_taker(1000.0, maker_fee_bps=16, taker_fee_bps=26)
        self.assertLess(cmp["maker_round_trip_cost_usd"], cmp["taker_round_trip_cost_usd"])
        self.assertGreater(cmp["savings_usd_from_preferring_maker"], 0)

    def test_savings_grows_with_spread_since_taker_pays_it_not_maker(self):
        tight = fees.compare_maker_vs_taker(1000.0, 16, 26, spread_bps=0)
        wide = fees.compare_maker_vs_taker(1000.0, 16, 26, spread_bps=20)
        self.assertGreater(wide["savings_usd_from_preferring_maker"], tight["savings_usd_from_preferring_maker"])


class TestEdgeClearsCosts(unittest.TestCase):
    def test_healthy_edge_clears(self):
        # $200 position, 35% take-profit target = $70 gross profit.
        # Maker-in/taker-out at 16/26bps = 42bps round trip = $8.40 cost.
        # $70 / $8.40 ~= 8.3x -- comfortably clears a 2x bar.
        result = fees.edge_clears_costs(200.0, take_profit_pct=0.35, entry_fee_bps=16, exit_fee_bps=26, min_edge_multiple=2.0)
        self.assertTrue(result["clears"])
        self.assertGreater(result["edge_to_cost_multiple"], 2.0)
        self.assertAlmostEqual(result["net_profit_at_take_profit_usd"], 70.0 - result["round_trip_cost_usd"])

    def test_thin_edge_does_not_clear(self):
        # a tiny take-profit target on a high-fee/wide-spread taker-in-taker-out
        # trade should fail the 2x bar
        result = fees.edge_clears_costs(200.0, take_profit_pct=0.01, entry_fee_bps=40, exit_fee_bps=40,
                                         entry_spread_bps=50, min_edge_multiple=2.0)
        self.assertFalse(result["clears"])

    def test_zero_cost_is_treated_as_infinite_multiple(self):
        result = fees.edge_clears_costs(200.0, take_profit_pct=0.10, entry_fee_bps=0, exit_fee_bps=0, min_edge_multiple=2.0)
        self.assertTrue(result["clears"])
        self.assertEqual(result["edge_to_cost_multiple"], float("inf"))

    def test_stop_loss_is_not_a_parameter(self):
        # deliberate: edge_clears_costs has no stop_loss_pct argument at all --
        # this test exists to catch a future accidental re-coupling of the two.
        import inspect
        params = inspect.signature(fees.edge_clears_costs).parameters
        self.assertNotIn("stop_loss_pct", params)


if __name__ == "__main__":
    unittest.main()
