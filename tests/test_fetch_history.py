"""Unit tests for backtest/fetch_history.py's daily resampling -- no network
access (operates on synthetic [timestamp_ms, value] series).
Run: python3 -m unittest discover -s tests -v"""
import http.client
import io
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import fetch_history as fh  # noqa: E402


def ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class TestResampleToDaily(unittest.TestCase):
    def test_empty_series_returns_empty(self):
        self.assertEqual(fh._resample_to_daily([]), [])

    def test_collapses_hourly_points_within_a_day_to_one(self):
        # CoinGecko's free API returns hourly granularity for any days<=90
        # request -- this is the exact shape that bug produces.
        day = datetime(2026, 1, 1, tzinfo=timezone.utc)
        series = [[ts_ms(day + timedelta(hours=h)), 100.0 + h] for h in range(24)]
        result = fh._resample_to_daily(series)
        self.assertEqual(len(result), 1)
        # last observation of the day wins (the day's "close")
        self.assertEqual(result[0][1], 100.0 + 23)

    def test_preserves_chronological_order_across_multiple_days(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        series = []
        for d in range(5):
            for h in range(0, 24, 4):
                series.append([ts_ms(base + timedelta(days=d, hours=h)), float(d * 10 + h)])
        result = fh._resample_to_daily(series)
        self.assertEqual(len(result), 5)
        # each day's kept value is the last (highest-hour) observation that day
        self.assertEqual([v for _, v in result], [0 + 20, 10 + 20, 20 + 20, 30 + 20, 40 + 20])
        # timestamps stay strictly increasing (chronological)
        timestamps = [t for t, _ in result]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_already_daily_series_is_unchanged(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        series = [[ts_ms(base + timedelta(days=d)), float(100 + d)] for d in range(10)]
        result = fh._resample_to_daily(series)
        self.assertEqual(len(result), 10)
        self.assertEqual([v for _, v in result], [v for _, v in series])


class TestFetchRetry(unittest.TestCase):
    """_fetch() retries transient server errors (429/502/503/504), not just
    429 -- mirrors RETRYABLE_HTTP_CODES in research/discover_candidates.py's
    _get_json(), which was broadened after RugCheck.xyz returned 502 for
    otherwise-clean candidates live. This file talks to a different upstream
    (CoinGecko) but was exposed to the same gap until fixed here too."""

    def _http_error(self, code):
        return urllib.error.HTTPError(url="http://x", code=code, msg="err", hdrs=None, fp=io.BytesIO(b""))

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_retries_on_502_then_succeeds(self, mock_urlopen, mock_sleep):
        success_resp = MagicMock()
        success_resp.read.return_value = b'{"ok": true}'
        success_resp.__enter__.return_value = success_resp
        mock_urlopen.side_effect = [self._http_error(502), success_resp]
        result = fh._fetch("http://x")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_retries_on_503_and_504_too(self, mock_urlopen, mock_sleep):
        for code in (503, 504):
            with self.subTest(code=code):
                success_resp = MagicMock()
                success_resp.read.return_value = b'{"ok": true}'
                success_resp.__enter__.return_value = success_resp
                mock_urlopen.side_effect = [self._http_error(code), success_resp]
                result = fh._fetch("http://x")
                self.assertEqual(result, {"ok": True})

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_non_retryable_error_raises_immediately(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = self._http_error(404)
        with self.assertRaises(urllib.error.HTTPError):
            fh._fetch("http://x")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_retries_on_raw_connection_reset_then_succeeds(self, mock_urlopen, mock_sleep):
        # Regression test: before this fix, _fetch() retried nothing but
        # HTTPError, so a raw connection-level failure (this environment's
        # urllib doesn't always wrap these as URLError -- see
        # research/discover_candidates.py's _get_json for the confirmed live
        # case) crashed the entire paper-trading/backtest run on the very
        # first attempt instead of retrying like a rate-limit does.
        success_resp = MagicMock()
        success_resp.read.return_value = b'{"ok": true}'
        success_resp.__enter__.return_value = success_resp
        mock_urlopen.side_effect = [
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            success_resp,
        ]
        result = fh._fetch("http://x")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_gives_up_after_exhausting_retries_on_persistent_connection_reset(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = http.client.RemoteDisconnected("Remote end closed connection without response")
        with self.assertRaises(http.client.RemoteDisconnected):
            fh._fetch("http://x", retries=3)
        self.assertEqual(mock_urlopen.call_count, 3)


if __name__ == "__main__":
    unittest.main()
