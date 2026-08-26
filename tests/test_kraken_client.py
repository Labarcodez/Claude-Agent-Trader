"""Unit tests for kraken/client.py -- request signing, response parsing,
retry/error handling. No real network calls.
Run: python3 -m unittest discover -s tests -v"""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kraken import client as kc  # noqa: E402


class TestParseDotenv(unittest.TestCase):
    """_parse_dotenv() is the pure-parsing half of _load_dotenv_if_present()
    (that function's file path is hardcoded to the repo root -- not
    something a test should depend on)."""

    def test_parses_a_simple_key_value_line(self):
        self.assertEqual(kc._parse_dotenv("KRAKEN_API_KEY=abc123"), {"KRAKEN_API_KEY": "abc123"})

    def test_strips_double_quotes(self):
        # a common .env convention this loader previously mishandled --
        # the literal quote characters silently became part of the value
        self.assertEqual(kc._parse_dotenv('KRAKEN_API_SECRET="abc123=="'), {"KRAKEN_API_SECRET": "abc123=="})

    def test_strips_single_quotes(self):
        self.assertEqual(kc._parse_dotenv("KRAKEN_API_KEY='abc123'"), {"KRAKEN_API_KEY": "abc123"})

    def test_mismatched_quotes_are_not_stripped(self):
        self.assertEqual(kc._parse_dotenv("KRAKEN_API_KEY='abc123\""), {"KRAKEN_API_KEY": "'abc123\""})

    def test_preserves_trailing_equals_padding_in_base64_secret(self):
        # base64 values routinely end in "=" or "==" padding -- partition()
        # splits on the FIRST "=" only, so this must not be mistaken for a
        # second key=value pair or truncated
        value = "qImoS+v2/Ka8XWq6pviU0huO67veiWR0Y+hZmeu7ypFK2MucOREZe9hG7L6odmwqdiybpdCZvh1zHuIkZJR/vw=="
        self.assertEqual(kc._parse_dotenv(f"KRAKEN_API_SECRET={value}"), {"KRAKEN_API_SECRET": value})

    def test_ignores_comment_and_blank_lines(self):
        text = "# a comment\n\nKRAKEN_API_KEY=abc123\n"
        self.assertEqual(kc._parse_dotenv(text), {"KRAKEN_API_KEY": "abc123"})

    def test_line_with_no_equals_sign_is_skipped(self):
        self.assertEqual(kc._parse_dotenv("not a valid line"), {})

    def test_multiple_lines(self):
        text = "KRAKEN_API_KEY=abc\nKRAKEN_API_SECRET=xyz\n"
        self.assertEqual(kc._parse_dotenv(text), {"KRAKEN_API_KEY": "abc", "KRAKEN_API_SECRET": "xyz"})


class TestSign(unittest.TestCase):
    """Verifies _sign() against Kraken's own published worked example
    (https://docs.kraken.com/rest/#section/Authentication) -- if this ever
    stops matching, the signing implementation is wrong, not the test."""

    def test_matches_krakens_published_example(self):
        api_sec = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
        data = {
            "nonce": "1616492376594",
            "ordertype": "limit",
            "pair": "XBTUSD",
            "price": 37500,
            "type": "buy",
            "volume": 1.25,
        }
        signature = kc._sign("/0/private/AddOrder", data, api_sec)
        self.assertEqual(signature, "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==")


class TestCheckErrors(unittest.TestCase):
    def test_no_error_key_is_fine(self):
        kc._check_errors({"result": {}}, "ctx")  # must not raise

    def test_empty_error_list_is_fine(self):
        kc._check_errors({"error": [], "result": {}}, "ctx")  # must not raise

    def test_nonempty_error_list_raises(self):
        with self.assertRaises(kc.KrakenAPIError):
            kc._check_errors({"error": ["EGeneral:Invalid arguments"]}, "ctx")


class TestIsRetryableKrakenError(unittest.TestCase):
    def test_rate_limit_is_retryable(self):
        self.assertTrue(kc._is_retryable_kraken_error(["EAPI:Rate limit exceeded"]))

    def test_invalid_arguments_is_not_retryable(self):
        self.assertFalse(kc._is_retryable_kraken_error(["EGeneral:Invalid arguments"]))

    def test_service_unavailable_is_retryable(self):
        self.assertTrue(kc._is_retryable_kraken_error(["EService:Unavailable"]))


class TestOhlc(unittest.TestCase):
    def _fake_response(self, payload: dict):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    @patch("kraken.client._request")
    def test_default_retry_policy_is_patient_not_the_cron_loop_override(self, mock_request):
        # A caller that doesn't override (e.g. backtest_all.py, via
        # fetch_ohlc_kraken()) must get a patient policy -- code review
        # found this silently inheriting an in-between default that was
        # neither patient nor paper_trading's explicit impatient override,
        # understating "backtest everything eligible"'s real coverage.
        mock_request.return_value = {"XXBTZUSD": [], "last": 0}
        kc.ohlc("XBTUSD", 180)
        _, kwargs = mock_request.call_args
        self.assertGreaterEqual(kwargs["retries"], 4)
        self.assertGreaterEqual(kwargs["backoff"], 10.0)

    @patch("urllib.request.urlopen")
    def test_parses_close_price_and_converts_timestamp_to_ms(self, mock_urlopen):
        mock_urlopen.return_value = self._fake_response({
            "error": [],
            "result": {"XXBTZUSD": [[1700000000, "50000", "51000", "49000", "50500", "50250", "10", 100]], "last": 1700000000},
        })
        result = kc.ohlc("XBTUSD", days=30)
        self.assertEqual(result, [[1700000000 * 1000, 50500.0]])

    @patch("urllib.request.urlopen")
    def test_truncates_to_requested_days(self, mock_urlopen):
        candles = [[i, "1", "1", "1", str(100 + i), "1", "1", 1] for i in range(10)]
        mock_urlopen.return_value = self._fake_response({"error": [], "result": {"XXBTZUSD": candles, "last": 9}})
        result = kc.ohlc("XBTUSD", days=3)
        self.assertEqual(len(result), 3)
        self.assertEqual(result, [[7000, 107.0], [8000, 108.0], [9000, 109.0]])

    @patch("urllib.request.urlopen")
    def test_kraken_error_raises(self, mock_urlopen):
        mock_urlopen.return_value = self._fake_response({"error": ["EQuery:Unknown asset pair"], "result": {}})
        with self.assertRaises(kc.KrakenAPIError):
            kc.ohlc("NOTAPAIR", days=30)


class TestTicker(unittest.TestCase):
    @patch("kraken.client._request")
    def test_batches_within_chunk_size_in_one_call(self, mock_request):
        mock_request.return_value = {}
        pairs = [f"P{i}USD" for i in range(kc.CHUNK_SIZE)]
        kc.ticker(pairs)
        self.assertEqual(mock_request.call_count, 1)

    @patch("time.sleep", return_value=None)
    @patch("kraken.client._request")
    def test_chunks_when_more_pairs_than_chunk_size(self, mock_request, mock_sleep):
        mock_request.return_value = {}
        pairs = [f"P{i}USD" for i in range(kc.CHUNK_SIZE + 5)]
        kc.ticker(pairs)
        expected_chunks = -(-len(pairs) // kc.CHUNK_SIZE)  # ceil division
        self.assertEqual(mock_request.call_count, expected_chunks)

    @patch("time.sleep", return_value=None)
    @patch("kraken.client._request")
    def test_paces_between_chunks_but_not_before_the_first(self, mock_request, mock_sleep):
        mock_request.return_value = {}
        pairs = [f"P{i}USD" for i in range(kc.CHUNK_SIZE + 5)]
        kc.ticker(pairs)
        self.assertEqual(mock_sleep.call_count, 1)  # 2 chunks -> 1 pace between them

    @patch("kraken.client._request")
    def test_one_bad_pair_falls_back_to_per_pair_without_losing_its_chunk_mates(self, mock_request):
        # A chunk-level Kraken error (e.g. one delisted/renamed pair in a
        # batch) must not blank out prices for every other pair in that
        # same chunk -- real risk found by code review: a stop-loss check
        # for a healthy position sharing a chunk with a bad pair would
        # otherwise silently not run.
        def side_effect(method, path, data=None, **kwargs):
            if data and "," in data.get("pair", ""):
                raise kc.KrakenAPIError("EQuery:Unknown asset pair")
            pair = data["pair"]
            if pair == "BADUSD":
                raise kc.KrakenAPIError("EQuery:Unknown asset pair")
            return {pair: {"a": ["1"], "b": ["1"]}}
        mock_request.side_effect = side_effect
        result = kc.ticker(["XBTUSD", "BADUSD", "ETHUSD"])
        self.assertIn("XBTUSD", result)
        self.assertIn("ETHUSD", result)
        self.assertNotIn("BADUSD", result)

    @patch("kraken.client._request")
    def test_dedupes_before_batching(self, mock_request):
        mock_request.return_value = {}
        kc.ticker(["XBTUSD", "XBTUSD", "XBTUSD"])
        called_data = mock_request.call_args[0][2]
        self.assertEqual(called_data["pair"].count("XBTUSD"), 1)


class TestBooleanSerialization(unittest.TestCase):
    """Regression test: urllib.parse.urlencode() stringifies a Python bool
    via str(), producing "True"/"False" rather than Kraken's documented
    lowercase "true"/"false". If Kraken's parser exact-matches on this,
    add_order(..., validate=True) -- the dry-run call propose_order.py's
    default (non---execute) path relies on to NEVER place a real order --
    could be silently treated as validate=false, placing a real order."""

    def _fake_response(self, payload: dict):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    @patch("urllib.request.urlopen")
    def test_validate_true_is_sent_as_lowercase_string(self, mock_urlopen):
        mock_urlopen.return_value = self._fake_response({"error": [], "result": {}})
        with patch.object(kc, "_api_key", return_value="k"), patch.object(kc, "_api_secret", return_value="c2VjcmV0"):
            kc._request("POST", "/0/private/AddOrder", {"pair": "XBTUSD", "validate": True}, private=True)
        body = mock_urlopen.call_args[0][0].data.decode()
        self.assertIn("validate=true", body)
        self.assertNotIn("validate=True", body)

    @patch("urllib.request.urlopen")
    def test_validate_false_is_sent_as_lowercase_string(self, mock_urlopen):
        mock_urlopen.return_value = self._fake_response({"error": [], "result": {}})
        with patch.object(kc, "_api_key", return_value="k"), patch.object(kc, "_api_secret", return_value="c2VjcmV0"):
            kc._request("POST", "/0/private/AddOrder", {"pair": "XBTUSD", "validate": False}, private=True)
        body = mock_urlopen.call_args[0][0].data.decode()
        self.assertIn("validate=false", body)


class TestRequestRetry(unittest.TestCase):
    def _http_error(self, code):
        return urllib.error.HTTPError(url="http://x", code=code, msg="err", hdrs=None, fp=io.BytesIO(b""))

    def _fake_response(self, payload: dict):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_retries_on_502_then_succeeds(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [self._http_error(502), self._fake_response({"error": [], "result": {"ok": True}})]
        result = kc._request("GET", "/0/public/Time")
        self.assertEqual(result, {"ok": True})

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_retries_on_kraken_rate_limit_error_body(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [
            self._fake_response({"error": ["EAPI:Rate limit exceeded"], "result": {}}),
            self._fake_response({"error": [], "result": {"ok": True}}),
        ]
        result = kc._request("GET", "/0/public/Time")
        self.assertEqual(result, {"ok": True})

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_non_retryable_kraken_error_raises_immediately(self, mock_urlopen, mock_sleep):
        mock_urlopen.return_value = self._fake_response({"error": ["EGeneral:Invalid arguments"], "result": {}})
        with self.assertRaises(kc.KrakenAPIError):
            kc._request("GET", "/0/public/Time")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_retries_on_raw_connection_reset(self, mock_urlopen, mock_sleep):
        import http.client
        mock_urlopen.side_effect = [
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            self._fake_response({"error": [], "result": {"ok": True}}),
        ]
        result = kc._request("GET", "/0/public/Time")
        self.assertEqual(result, {"ok": True})


if __name__ == "__main__":
    unittest.main()
