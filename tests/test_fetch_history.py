"""Unit tests for backtest/fetch_history.py's daily resampling -- no network
access (operates on synthetic [timestamp_ms, value] series).
Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
