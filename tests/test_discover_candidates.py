"""Unit tests for research/discover_candidates.py's Kraken safety/tier logic
-- mocked Kraken/CoinGecko responses, no real network calls.
Run: python3 -m unittest discover -s tests -v"""
import argparse
import http.client
import io
import json
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from research import discover_candidates as disco  # noqa: E402


def make_args(**overrides):
    defaults = dict(
        min_24h_volume_usd=1_000_000,
        max_spread_bps=50.0,
        blue_chip_mcap_usd=50_000_000_000,
        blue_chip_volume_usd=100_000_000,
        established_mcap_usd=1_000_000_000,
        established_volume_usd=10_000_000,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def good_pair_data(**overrides):
    data = {
        "pair": "XBTUSD", "base": "XBT", "altname": "XBTUSD", "wsname": "XBT/USD",
        "status": "online", "price_usd": 50_000.0, "volume_24h_usd": 5_000_000, "spread_bps": 5.0,
    }
    data.update(overrides)
    return data


class TestEvaluateCandidateSafetyChecks(unittest.TestCase):
    def test_good_pair_is_eligible(self):
        result = disco.evaluate_candidate("XBTUSD", good_pair_data(), make_args())
        self.assertTrue(result["eligible"], result["reasons_fail"])

    def test_offline_status_rejects(self):
        result = disco.evaluate_candidate("XBTUSD", good_pair_data(status="cancel_only"), make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("status" in r for r in result["reasons_fail"]))

    def test_zero_price_rejects(self):
        result = disco.evaluate_candidate("XBTUSD", good_pair_data(price_usd=0), make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("price" in r for r in result["reasons_fail"]))

    def test_low_volume_rejects(self):
        result = disco.evaluate_candidate("XBTUSD", good_pair_data(volume_24h_usd=1_000), make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("volume" in r for r in result["reasons_fail"]))

    def test_wide_spread_rejects(self):
        result = disco.evaluate_candidate("XBTUSD", good_pair_data(spread_bps=999.0), make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("spread" in r for r in result["reasons_fail"]))

    def test_missing_spread_rejects(self):
        result = disco.evaluate_candidate("XBTUSD", good_pair_data(spread_bps=None), make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("spread" in r for r in result["reasons_fail"]))

    def test_ineligible_pair_gets_no_tier(self):
        result = disco.evaluate_candidate("XBTUSD", good_pair_data(volume_24h_usd=0), make_args())
        self.assertIsNone(result["tier"])

    def test_reports_data_fields_regardless_of_eligibility(self):
        result = disco.evaluate_candidate("XBTUSD", good_pair_data(), make_args(), mcap_usd=123.0)
        self.assertEqual(result["data"]["mcap_usd"], 123.0)
        self.assertEqual(result["data"]["price_usd"], 50_000.0)
        self.assertEqual(result["symbol"], "XBTUSD")


class TestClassifyTier(unittest.TestCase):
    """classify_tier() requires BOTH market cap AND 24h volume to clear a
    tier's bar -- mirrors the old pipeline's "mcap AND holder_count"
    two-factor requirement, just with Kraken's own available signals."""

    def test_blue_chip_requires_both_mcap_and_volume(self):
        args = make_args()
        self.assertEqual(disco.classify_tier(60_000_000_000, 200_000_000, args), "blue_chip")

    def test_high_mcap_alone_is_not_enough_for_blue_chip(self):
        # mcap clears blue_chip's bar, but volume only clears established's --
        # both factors must independently clear the SAME tier's bar
        args = make_args()
        self.assertEqual(disco.classify_tier(60_000_000_000, 20_000_000, args), "established")

    def test_established_requires_both_mcap_and_volume(self):
        args = make_args()
        self.assertEqual(disco.classify_tier(2_000_000_000, 20_000_000, args), "established")

    def test_falls_back_to_emerging_when_neither_tier_clears(self):
        args = make_args()
        self.assertEqual(disco.classify_tier(1_000_000, 1_000, args), "emerging")

    def test_zero_mcap_is_emerging(self):
        # the fallback for any base asset not in KRAKEN_TO_COINGECKO_ID
        args = make_args()
        self.assertEqual(disco.classify_tier(0, 50_000_000, args), "emerging")


class TestKrakenBaseAltname(unittest.TestCase):
    """_kraken_base_altname() normalizes Kraken's legacy X/Z-prefixed asset
    codes (e.g. "XXBT" -> "XBT") so KRAKEN_TO_COINGECKO_ID lookups work.
    Prefers an authoritative asset_altnames map (from
    fetch_kraken_asset_altnames()) when given one; falls back to a
    length/prefix heuristic only when it isn't -- see the function's own
    docstring for why code review flagged the heuristic-only version as a
    silent mis-tiering risk."""

    def test_uses_authoritative_map_when_given(self):
        # a code that would defeat the heuristic (doesn't start with X/Z)
        # resolves correctly when the real Kraken mapping is provided
        self.assertEqual(disco._kraken_base_altname("WEIRDCODE", {"WEIRDCODE": "SOL"}), "SOL")

    def test_authoritative_map_takes_precedence_over_heuristic(self):
        # even for a code the heuristic WOULD get right, the authoritative
        # map is checked first
        self.assertEqual(disco._kraken_base_altname("XXDG", {"XXDG": "XDG"}), "XDG")

    def test_falls_back_to_heuristic_when_code_not_in_map(self):
        self.assertEqual(disco._kraken_base_altname("XXDG", {"SOMETHING_ELSE": "FOO"}), "XDG")

    def test_falls_back_to_heuristic_when_no_map_given(self):
        self.assertEqual(disco._kraken_base_altname("XXDG"), "XDG")

    def test_strips_leading_x_prefix(self):
        self.assertEqual(disco._kraken_base_altname("XXDG"), "XDG")

    def test_already_bare_code_is_unchanged(self):
        self.assertEqual(disco._kraken_base_altname("SOL"), "SOL")

    def test_unrecognized_code_falls_through_unchanged(self):
        self.assertEqual(disco._kraken_base_altname("NOTAREALCODE"), "NOTAREALCODE")

    def test_short_code_is_not_stripped(self):
        # guards against over-eager stripping of a genuinely 3-letter code
        # that happens to start with X or Z but isn't a legacy-prefixed one
        self.assertEqual(disco._kraken_base_altname("XRP"), "XRP")

    def test_none_base_code_returns_empty_string_not_a_crash(self):
        # a pair whose AssetPairs entry is missing "base" entirely must
        # degrade to a lookup miss ("emerging" tier), not an uncaught
        # TypeError from len(None) -- real gap, found by code review
        self.assertEqual(disco._kraken_base_altname(None), "")
        self.assertEqual(disco._kraken_base_altname(None, {"XXDG": "XDG"}), "")


class TestFetchKrakenAssetAltnames(unittest.TestCase):
    @patch("kraken.client.assets")
    def test_builds_code_to_altname_map(self, mock_assets):
        mock_assets.return_value = {"XXBT": {"altname": "XBT"}, "ZUSD": {"altname": "USD"}}
        self.assertEqual(disco.fetch_kraken_asset_altnames(), {"XXBT": "XBT", "ZUSD": "USD"})

    @patch("kraken.client.assets")
    def test_skips_entries_with_no_altname(self, mock_assets):
        mock_assets.return_value = {"XXBT": {"altname": "XBT"}, "WEIRD": {}}
        self.assertEqual(disco.fetch_kraken_asset_altnames(), {"XXBT": "XBT"})

    @patch("kraken.client.assets")
    def test_kraken_api_error_degrades_to_empty_dict(self, mock_assets):
        from kraken.client import KrakenAPIError
        mock_assets.side_effect = KrakenAPIError("boom")
        self.assertEqual(disco.fetch_kraken_asset_altnames(), {})


class TestGatherCandidatesFiltering(unittest.TestCase):
    """gather_candidates() must exclude dark-pool pairs, non-USD-quoted
    pairs, offline pairs, and fiat/stablecoin bases -- only what's left is a
    meaningful discovery candidate."""

    @patch("kraken.client.assets")
    @patch("kraken.client.ticker")
    @patch("kraken.client.asset_pairs")
    def test_excludes_dark_pool_pairs(self, mock_pairs, mock_ticker, mock_assets):
        mock_assets.return_value = {}
        mock_pairs.return_value = {
            "XBTUSD": {"base": "XBT", "quote": "ZUSD", "status": "online", "altname": "XBTUSD"},
            "XBTUSD.d": {"base": "XBT", "quote": "ZUSD", "status": "online", "altname": "XBTUSD.d"},
        }
        mock_ticker.return_value = {}
        disco.gather_candidates(make_args())
        called_pairs = mock_ticker.call_args[0][0]
        self.assertIn("XBTUSD", called_pairs)
        self.assertNotIn("XBTUSD.d", called_pairs)

    @patch("kraken.client.assets")
    @patch("kraken.client.ticker")
    @patch("kraken.client.asset_pairs")
    def test_excludes_offline_pairs(self, mock_pairs, mock_ticker, mock_assets):
        mock_assets.return_value = {}
        mock_pairs.return_value = {
            "XBTUSD": {"base": "XBT", "quote": "ZUSD", "status": "online", "altname": "XBTUSD"},
            "DEADUSD": {"base": "DEAD", "quote": "ZUSD", "status": "cancel_only", "altname": "DEADUSD"},
        }
        mock_ticker.return_value = {}
        disco.gather_candidates(make_args())
        called_pairs = mock_ticker.call_args[0][0]
        self.assertIn("XBTUSD", called_pairs)
        self.assertNotIn("DEADUSD", called_pairs)

    @patch("kraken.client.assets")
    @patch("kraken.client.ticker")
    @patch("kraken.client.asset_pairs")
    def test_excludes_non_usd_quoted_pairs(self, mock_pairs, mock_ticker, mock_assets):
        mock_assets.return_value = {}
        mock_pairs.return_value = {
            "XBTUSD": {"base": "XBT", "quote": "ZUSD", "status": "online", "altname": "XBTUSD"},
            "XBTEUR": {"base": "XBT", "quote": "ZEUR", "status": "online", "altname": "XBTEUR"},
        }
        mock_ticker.return_value = {}
        disco.gather_candidates(make_args())
        called_pairs = mock_ticker.call_args[0][0]
        self.assertIn("XBTUSD", called_pairs)
        self.assertNotIn("XBTEUR", called_pairs)

    @patch("kraken.client.assets")
    @patch("kraken.client.ticker")
    @patch("kraken.client.asset_pairs")
    def test_excludes_stablecoin_and_fiat_bases(self, mock_pairs, mock_ticker, mock_assets):
        mock_assets.return_value = {}
        mock_pairs.return_value = {
            "XBTUSD": {"base": "XBT", "quote": "ZUSD", "status": "online", "altname": "XBTUSD"},
            "USDTZUSD": {"base": "USDT", "quote": "ZUSD", "status": "online", "altname": "USDTUSD"},
            "ZEURZUSD": {"base": "ZEUR", "quote": "ZUSD", "status": "online", "altname": "EURUSD"},
        }
        mock_ticker.return_value = {}
        disco.gather_candidates(make_args())
        called_pairs = mock_ticker.call_args[0][0]
        self.assertIn("XBTUSD", called_pairs)
        self.assertNotIn("USDTZUSD", called_pairs)
        self.assertNotIn("ZEURZUSD", called_pairs)

    @patch("kraken.client.assets")
    @patch("kraken.client.ticker")
    @patch("kraken.client.asset_pairs")
    def test_computes_price_volume_and_spread_from_ticker(self, mock_pairs, mock_ticker, mock_assets):
        mock_assets.return_value = {}
        mock_pairs.return_value = {"XBTUSD": {"base": "XBT", "quote": "ZUSD", "status": "online", "altname": "XBTUSD"}}
        mock_ticker.return_value = {
            "XBTUSD": {"a": ["101.0", "1", "1"], "b": ["99.0", "1", "1"], "c": ["100.0", "1"],
                       "v": ["10", "1000"], "p": ["100.0", "100.0"]},
        }
        candidates = disco.gather_candidates(make_args())
        data = candidates["XBTUSD"]
        self.assertAlmostEqual(data["price_usd"], 100.0)
        self.assertAlmostEqual(data["volume_24h_usd"], 1000 * 100.0)
        self.assertAlmostEqual(data["spread_bps"], (101.0 - 99.0) / 100.0 * 10_000)

    @patch("kraken.client.assets")
    @patch("kraken.client.ticker")
    @patch("kraken.client.asset_pairs")
    def test_stores_authoritative_base_altname_from_assets_endpoint(self, mock_pairs, mock_ticker, mock_assets):
        mock_assets.return_value = {"XXBT": {"altname": "XBT"}}
        mock_pairs.return_value = {"XXBTZUSD": {"base": "XXBT", "quote": "ZUSD", "status": "online", "altname": "XBTUSD"}}
        mock_ticker.return_value = {
            "XXBTZUSD": {"a": ["101.0", "1", "1"], "b": ["99.0", "1", "1"], "c": ["100.0", "1"],
                         "v": ["10", "1000"], "p": ["100.0", "100.0"]},
        }
        candidates = disco.gather_candidates(make_args())
        self.assertEqual(candidates["XXBTZUSD"]["base_altname"], "XBT")

    @patch("kraken.client.assets")
    @patch("kraken.client.ticker")
    @patch("kraken.client.asset_pairs")
    def test_empty_book_reports_no_spread_rather_than_a_false_zero(self, mock_pairs, mock_ticker, mock_assets):
        # ask==bid==0 (no live quote) -- mid correctly falls back to last-trade
        # price, but spread_bps must be None (unmeasurable), not a false "0.0bps
        # perfectly tight" reading computed from (0-0)/last. Real bug, found by
        # code review: evaluate_candidate()'s safety gate only rejects on
        # spread_bps is None, so a computed 0.0 sailed straight through.
        mock_assets.return_value = {}
        mock_pairs.return_value = {"XBTUSD": {"base": "XBT", "quote": "ZUSD", "status": "online", "altname": "XBTUSD"}}
        mock_ticker.return_value = {
            "XBTUSD": {"a": ["0", "1", "1"], "b": ["0", "1", "1"], "c": ["100.0", "1"],
                       "v": ["10", "1000"], "p": ["100.0", "100.0"]},
        }
        candidates = disco.gather_candidates(make_args())
        self.assertIsNone(candidates["XBTUSD"]["spread_bps"])
        self.assertEqual(candidates["XBTUSD"]["price_usd"], 100.0)  # mid still falls back to last, that part is correct

    @patch("kraken.client.assets")
    @patch("kraken.client.ticker")
    @patch("kraken.client.asset_pairs")
    def test_pair_missing_from_ticker_response_is_skipped(self, mock_pairs, mock_ticker, mock_assets):
        mock_assets.return_value = {}
        mock_pairs.return_value = {"XBTUSD": {"base": "XBT", "quote": "ZUSD", "status": "online", "altname": "XBTUSD"}}
        mock_ticker.return_value = {}  # Ticker didn't return this pair
        candidates = disco.gather_candidates(make_args())
        self.assertEqual(candidates, {})


class TestMarketCapsForPairs(unittest.TestCase):
    """market_caps_for_pairs() -- the consolidated replacement for what used
    to be three near-identical hand-written coingecko-id-resolution blocks
    in this module, backtest/backtest_all.py, and
    paper_trading/run_paper_cycle.py (code review, 2026-08-26: they'd
    already started diverging)."""

    def _candidates(self, **overrides):
        base = {
            "XBTUSD": {"base_altname": "XBT"},
            "UNKNOWNUSD": {"base_altname": "TOTALLYUNKNOWN"},
        }
        base.update(overrides)
        return base

    @patch("research.discover_candidates.fetch_market_caps_usd")
    def test_maps_result_back_to_pair_names(self, mock_fetch):
        mock_fetch.return_value = {"bitcoin": 1_500_000_000_000}
        result = disco.market_caps_for_pairs(self._candidates(), ["XBTUSD", "UNKNOWNUSD"])
        self.assertEqual(result["XBTUSD"], 1_500_000_000_000)

    @patch("research.discover_candidates.fetch_market_caps_usd")
    def test_unknown_base_altname_defaults_to_zero(self, mock_fetch):
        mock_fetch.return_value = {}
        result = disco.market_caps_for_pairs(self._candidates(), ["UNKNOWNUSD"])
        self.assertEqual(result["UNKNOWNUSD"], 0)

    @patch("research.discover_candidates.fetch_market_caps_usd")
    def test_calls_fetch_market_caps_once_for_the_whole_pair_list(self, mock_fetch):
        # fetch_market_caps_usd() already dedupes internally -- this just
        # confirms market_caps_for_pairs() doesn't fetch per-pair
        mock_fetch.return_value = {}
        candidates = {"A": {"base_altname": "XBT"}, "B": {"base_altname": "XBT"}}
        disco.market_caps_for_pairs(candidates, ["A", "B"])
        self.assertEqual(mock_fetch.call_count, 1)
        mock_fetch.assert_called_once_with(["bitcoin", "bitcoin"])


class TestFetchMarketCapsUsd(unittest.TestCase):
    @patch("research.discover_candidates._get_json")
    def test_empty_ids_list_short_circuits_without_a_call(self, mock_get):
        result = disco.fetch_market_caps_usd([])
        self.assertEqual(result, {})
        mock_get.assert_not_called()

    @patch("research.discover_candidates._get_json")
    def test_maps_id_to_market_cap(self, mock_get):
        mock_get.return_value = [{"id": "bitcoin", "market_cap": 1_500_000_000_000}]
        result = disco.fetch_market_caps_usd(["bitcoin"])
        self.assertEqual(result, {"bitcoin": 1_500_000_000_000})

    @patch("research.discover_candidates._get_json")
    def test_one_call_for_multiple_ids(self, mock_get):
        mock_get.return_value = []
        disco.fetch_market_caps_usd(["bitcoin", "ethereum", "bitcoin"])  # duplicate deduped
        self.assertEqual(mock_get.call_count, 1)
        called_url = mock_get.call_args[0][0]
        self.assertIn("bitcoin", called_url)
        self.assertIn("ethereum", called_url)

    @patch("research.discover_candidates._get_json")
    def test_none_response_degrades_to_empty_not_a_crash(self, mock_get):
        mock_get.return_value = None
        self.assertEqual(disco.fetch_market_caps_usd(["bitcoin"]), {})


class TestGetJsonRetry(unittest.TestCase):
    """_get_json() (CoinGecko-only now -- Kraken's own retry lives in
    kraken/client.py) retries transient server errors (429/502/503/504) and
    raw connection resets, mirroring kraken/client.py's _request()."""

    def _http_error(self, code):
        return urllib.error.HTTPError(url="http://x", code=code, msg="err", hdrs=None, fp=io.BytesIO(b""))

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_retries_on_502_then_succeeds(self, mock_urlopen, mock_sleep):
        from unittest.mock import MagicMock
        success_resp = MagicMock()
        success_resp.read.return_value = b'{"ok": true}'
        success_resp.__enter__.return_value = success_resp
        mock_urlopen.side_effect = [self._http_error(502), success_resp]
        result = disco._get_json("http://x")
        self.assertEqual(result, {"ok": True})

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_404_returns_none_without_retrying(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = self._http_error(404)
        result = disco._get_json("http://x")
        self.assertIsNone(result)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_retries_on_raw_connection_reset(self, mock_urlopen, mock_sleep):
        from unittest.mock import MagicMock
        success_resp = MagicMock()
        success_resp.read.return_value = b'{"ok": true}'
        success_resp.__enter__.return_value = success_resp
        mock_urlopen.side_effect = [
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            success_resp,
        ]
        result = disco._get_json("http://x")
        self.assertEqual(result, {"ok": True})


class TestDiscoveryHistoryPersistence(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parent / "_tmp_discovery_history_test.json"
        if self.path.exists():
            self.path.unlink()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(disco.load_discovery_history(self.path), {})

    def test_corrupted_file_degrades_to_empty_not_a_crash(self):
        self.path.write_text("{not valid json")
        self.assertEqual(disco.load_discovery_history(self.path), {})

    def test_round_trip_through_save_and_load(self):
        history = {"XBTUSD": {"symbol": "XBTUSD", "snapshots": [{"ts": "2026-01-01T00:00:00+00:00"}]}}
        disco.save_discovery_history(history, self.path)
        self.assertEqual(disco.load_discovery_history(self.path), history)


class TestRecordAndComputeTrend(unittest.TestCase):
    def test_first_sighting_has_no_growth_rate(self):
        history = {}
        trend = disco.record_and_compute_trend("XBTUSD", "XBTUSD", {"volume_24h_usd": 1000, "spread_bps": 5.0}, history)
        self.assertEqual(trend["cycles_seen"], 1)
        self.assertIsNone(trend["volume_growth_usd_per_hour"])
        self.assertIsNone(trend["spread_bps_delta"])

    def test_computes_volume_growth_rate_against_oldest_snapshot(self):
        history = {}
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        disco.record_and_compute_trend("XBTUSD", "XBTUSD", {"volume_24h_usd": 1000, "spread_bps": 5.0}, history, now=t0)
        t1 = t0 + timedelta(hours=2)
        trend = disco.record_and_compute_trend("XBTUSD", "XBTUSD", {"volume_24h_usd": 5000, "spread_bps": 3.0}, history, now=t1)
        self.assertAlmostEqual(trend["volume_growth_usd_per_hour"], (5000 - 1000) / 2)
        self.assertAlmostEqual(trend["spread_bps_delta"], 3.0 - 5.0)

    def test_history_window_is_bounded(self):
        history = {}
        for i in range(disco.MAX_HISTORY_SNAPSHOTS_PER_MINT + 5):
            disco.record_and_compute_trend("XBTUSD", "XBTUSD", {"volume_24h_usd": i, "spread_bps": 1.0}, history,
                                            now=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i))
        self.assertEqual(len(history["XBTUSD"]["snapshots"]), disco.MAX_HISTORY_SNAPSHOTS_PER_MINT)

    def test_near_zero_elapsed_time_does_not_produce_a_wild_rate(self):
        history = {}
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        disco.record_and_compute_trend("XBTUSD", "XBTUSD", {"volume_24h_usd": 1000, "spread_bps": 5.0}, history, now=t0)
        t1 = t0 + timedelta(seconds=1)  # far under the 0.05h guard
        trend = disco.record_and_compute_trend("XBTUSD", "XBTUSD", {"volume_24h_usd": 5000, "spread_bps": 5.0}, history, now=t1)
        self.assertIsNone(trend["volume_growth_usd_per_hour"])


if __name__ == "__main__":
    unittest.main()
