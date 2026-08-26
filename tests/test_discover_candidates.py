"""Unit tests for research/discover_candidates.py's safety/tier logic --
synthetic Kraken AssetPairs/Ticker-shaped dicts, no real network calls.
Run: python3 -m unittest discover -s tests -v"""
import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from research import discover_candidates as disco  # noqa: E402


def make_args(**overrides):
    defaults = dict(
        min_24h_quote_volume_usd=500_000,
        max_spread_bps=50,
        min_price_usd=0.000001,
        blue_chip_min_volume_usd=100_000_000,
        blue_chip_max_spread_bps=10,
        established_min_volume_usd=10_000_000,
        established_max_spread_bps=25,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def good_pair_info(**overrides):
    info = {
        "altname": "GOODUSD",
        "wsname": "GOOD/USD",
        "base": "GOOD",
        "quote": "ZUSD",
        "status": "online",
        "ordermin": "1",
        "costmin": "0.5",
        "leverage_buy": [],
        "leverage_sell": [],
    }
    info.update(overrides)
    return info


def good_ticker(**overrides):
    # c=last trade [price, lot volume], b=bid, a=ask, v=volume [today, 24h], p=vwap [today, 24h]
    t = {
        "c": ["10.00", "1.0"],
        "b": ["9.99", "5.0", "5.0"],
        "a": ["10.01", "5.0", "5.0"],
        "v": ["500000", "2000000"],
        "p": ["10.00", "10.00"],
    }
    t.update(overrides)
    return t


def candidate(pair_overrides=None, ticker_overrides=None, no_ticker=False):
    return {
        "pair": good_pair_info(**(pair_overrides or {})),
        "ticker": None if no_ticker else good_ticker(**(ticker_overrides or {})),
    }


class TestEvaluateCandidateSafetyChecks(unittest.TestCase):
    def test_good_pair_is_eligible(self):
        result = disco.evaluate_candidate("GOODUSD", candidate(), make_args())
        self.assertTrue(result["eligible"], result["reasons_fail"])

    def test_offline_status_rejects(self):
        result = disco.evaluate_candidate("GOODUSD", candidate(pair_overrides={"status": "cancel_only"}), make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("status" in r for r in result["reasons_fail"]))

    def test_missing_ticker_rejects(self):
        result = disco.evaluate_candidate("GOODUSD", candidate(no_ticker=True), make_args())
        self.assertFalse(result["eligible"])
        self.assertIsNone(result["tier"])

    def test_low_24h_quote_volume_rejects(self):
        # 2,000,000 * vwap(10.00) = $20,000,000 notional normally -- shrink volume to drop below the $500k floor
        result = disco.evaluate_candidate("GOODUSD", candidate(ticker_overrides={"v": ["1000", "1000"]}), make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("quote volume" in r for r in result["reasons_fail"]))

    def test_wide_spread_rejects(self):
        result = disco.evaluate_candidate(
            "GOODUSD", candidate(ticker_overrides={"b": ["9.00", "5.0", "5.0"], "a": ["11.00", "5.0", "5.0"]}), make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("spread" in r for r in result["reasons_fail"]))

    def test_zero_price_rejects(self):
        result = disco.evaluate_candidate("GOODUSD", candidate(ticker_overrides={"c": ["0", "1.0"]}), make_args())
        self.assertFalse(result["eligible"])

    def test_malformed_ticker_rejects_without_raising(self):
        result = disco.evaluate_candidate("GOODUSD", candidate(ticker_overrides={"c": None}), make_args())
        self.assertFalse(result["eligible"])
        self.assertIsNone(result["tier"])


class TestTierClassification(unittest.TestCase):
    def test_blue_chip_tier(self):
        # 24h volume 5,000,000 base * vwap 30 = $150,000,000, tight spread
        result = disco.evaluate_candidate(
            "GOODUSD",
            candidate(
                ticker_overrides={"c": ["30.00", "1.0"], "b": ["29.99", "5", "5"], "a": ["30.01", "5", "5"],
                                   "v": ["5000000", "5000000"], "p": ["30.00", "30.00"]},
            ),
            make_args(),
        )
        self.assertEqual(result["tier"], "blue_chip")

    def test_established_tier(self):
        # $50,000,000 notional, 15bps spread -- clears established's 25bps cap
        # but not blue_chip's tighter 10bps cap
        result = disco.evaluate_candidate(
            "GOODUSD",
            candidate(
                ticker_overrides={"c": ["10.00", "1.0"], "b": ["9.9925", "5", "5"], "a": ["10.0075", "5", "5"],
                                   "v": ["5000000", "5000000"], "p": ["10.00", "10.00"]},
            ),
            make_args(),
        )
        self.assertEqual(result["tier"], "established")

    def test_emerging_tier_for_small_but_safe_pair(self):
        # just above the $500k safety floor, well under established's $10M
        result = disco.evaluate_candidate(
            "GOODUSD",
            candidate(ticker_overrides={"v": ["100000", "100000"], "p": ["10.00", "10.00"]}),
            make_args(),
        )
        self.assertEqual(result["tier"], "emerging")

    def test_no_tier_assigned_to_rejected_candidate(self):
        result = disco.evaluate_candidate("GOODUSD", candidate(pair_overrides={"status": "delisted"}), make_args())
        self.assertIsNone(result["tier"])


class TestGatherCandidatesFiltering(unittest.TestCase):
    """gather_candidates()'s prefilter (status/quote-currency/leveraged-token
    pattern) runs before any Ticker call -- tested here against synthetic
    AssetPairs-shaped dicts, independent of evaluate_candidate."""

    def test_leveraged_token_pattern_matches_expected_suffixes(self):
        for bad in ("BTC3L", "ETH3S", "SOL5L", "DOGE2S"):
            self.assertIsNotNone(disco.LEVERAGED_TOKEN_PATTERN.search(bad), bad)
        for ok in ("BTC", "ETH", "SOL", "USDT"):
            self.assertIsNone(disco.LEVERAGED_TOKEN_PATTERN.search(ok), ok)

    def test_allowed_quote_currencies(self):
        self.assertEqual(disco.ALLOWED_QUOTE_CURRENCIES, {"USD", "USDT", "USDC"})

    def test_legacy_asset_prefix_strips_x_and_z(self):
        self.assertEqual(disco.LEGACY_ASSET_PREFIX.sub("", "XXBT"), "XBT")
        self.assertEqual(disco.LEGACY_ASSET_PREFIX.sub("", "ZUSD"), "USD")
        self.assertEqual(disco.LEGACY_ASSET_PREFIX.sub("", "SOL"), "SOL")   # no legacy prefix to strip


if __name__ == "__main__":
    unittest.main()
