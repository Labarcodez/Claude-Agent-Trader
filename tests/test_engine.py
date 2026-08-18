"""Unit tests for backtest/engine.py's simulation mechanics -- deterministic
synthetic strategies, no cached files or network access needed.
Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import engine  # noqa: E402


def buy_at_5_sell_at_10(closes, i, state):
    """Deterministic strategy for testing engine mechanics in isolation from
    any real signal logic."""
    if i == 5:
        return "buy"
    if i == 10:
        return "sell"
    return "hold"


def always_hold(closes, i, state):
    return "hold"


def series_of(prices: list[float]):
    return [(i * 1000, p) for i, p in enumerate(prices)]


class TestSimulateMechanics(unittest.TestCase):
    def test_profitable_round_trip_no_fees(self):
        prices = [100.0] * 20
        prices[10] = 110.0  # +10% from the entry at index 5
        result = engine._simulate(series_of(prices), "test", buy_at_5_sell_at_10, fee_bps=0, slippage_bps=0)
        self.assertEqual(len(result.trades), 2)
        self.assertAlmostEqual(result.ending_equity, 1.10, places=6)
        self.assertAlmostEqual(result.total_return_pct, 10.0, places=4)

    def test_fees_strictly_reduce_return_vs_no_fee_baseline(self):
        prices = [100.0] * 20
        prices[10] = 110.0
        no_fee = engine._simulate(series_of(prices), "test", buy_at_5_sell_at_10, fee_bps=0, slippage_bps=0)
        with_fee = engine._simulate(series_of(prices), "test", buy_at_5_sell_at_10, fee_bps=100, slippage_bps=0)
        self.assertLess(with_fee.ending_equity, no_fee.ending_equity)

    def test_losing_trade_produces_negative_drawdown(self):
        prices = [100.0] * 20
        prices[10] = 90.0
        result = engine._simulate(series_of(prices), "test", buy_at_5_sell_at_10, fee_bps=0, slippage_bps=0)
        self.assertLess(result.max_drawdown_pct, 0)

    def test_win_rate_100_pct_on_single_winning_round_trip(self):
        prices = [100.0] * 20
        prices[10] = 110.0
        result = engine._simulate(series_of(prices), "test", buy_at_5_sell_at_10, fee_bps=0, slippage_bps=0)
        self.assertEqual(result.win_rate_pct, 100.0)

    def test_win_rate_0_pct_on_single_losing_round_trip(self):
        prices = [100.0] * 20
        prices[10] = 90.0
        result = engine._simulate(series_of(prices), "test", buy_at_5_sell_at_10, fee_bps=0, slippage_bps=0)
        self.assertEqual(result.win_rate_pct, 0.0)

    def test_win_rate_none_with_no_closed_trades(self):
        prices = [100.0] * 20
        result = engine._simulate(series_of(prices), "test", always_hold, fee_bps=0, slippage_bps=0)
        self.assertIsNone(result.win_rate_pct)
        self.assertEqual(result.num_round_trips, 0)

    def test_flat_series_zero_return(self):
        prices = [100.0] * 20
        result = engine._simulate(series_of(prices), "test", always_hold, fee_bps=30, slippage_bps=50)
        self.assertEqual(result.total_return_pct, 0.0)

    def test_start_index_skips_recording_before_it(self):
        prices = [100.0 + i for i in range(100)]
        result = engine._simulate(series_of(prices), "test", always_hold, fee_bps=0, slippage_bps=0, start_index=50)
        self.assertEqual(len(result.equity_curve), 50)

    def test_open_position_marked_to_market_at_series_end(self):
        # buys at index 5 and never sells -- ending_equity should reflect the
        # final price's return relative to the entry, not just cash sitting idle
        prices = [100.0] * 20
        prices[-1] = 120.0

        def buy_and_hold(closes, i, state):
            return "buy" if i == 5 else "hold"

        result = engine._simulate(series_of(prices), "test", buy_and_hold, fee_bps=0, slippage_bps=0)
        self.assertGreater(result.ending_equity, 1.0)


if __name__ == "__main__":
    unittest.main()
