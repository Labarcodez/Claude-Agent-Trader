"""Unit tests for backtest/strategies.py -- synthetic price series with known
properties, no network access. Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import strategies as strat  # noqa: E402


class TestIndicators(unittest.TestCase):
    def test_sma_basic(self):
        self.assertEqual(strat.sma([1, 2, 3, 4, 5], 5), 3)

    def test_sma_none_when_insufficient_data(self):
        self.assertIsNone(strat.sma([1, 2], 5))

    def test_rsi_all_gains_is_100(self):
        closes = [float(i) for i in range(1, 30)]  # strictly increasing
        self.assertEqual(strat.rsi(closes, 14), 100.0)

    def test_rsi_all_losses_is_0(self):
        closes = [float(30 - i) for i in range(30)]  # strictly decreasing
        self.assertEqual(strat.rsi(closes, 14), 0.0)

    def test_rsi_none_when_insufficient_data(self):
        self.assertIsNone(strat.rsi([1, 2, 3], 14))

    def test_realized_vol_zero_for_flat_series(self):
        closes = [100.0] * 25
        self.assertEqual(strat.realized_vol(closes, 20), 0.0)

    def test_realized_vol_none_when_insufficient_data(self):
        self.assertIsNone(strat.realized_vol([1, 2, 3], 20))

    def test_realized_vol_positive_for_noisy_series(self):
        closes = [100.0, 105.0, 98.0, 103.0, 97.0] * 5
        vol = strat.realized_vol(closes, 20)
        self.assertGreater(vol, 0)


class TestSmaCrossover(unittest.TestCase):
    def test_buy_appears_on_upward_crossover(self):
        # flat, then a sharp sustained ramp -- the fast SMA must cross above the slow one
        closes = [100.0] * 30 + [100 + i * 3 for i in range(1, 20)]
        signals = [strat.sma_crossover(closes, i, {}) for i in range(len(closes))]
        self.assertIn("buy", signals)

    def test_hold_on_perfectly_flat_series(self):
        closes = [100.0] * 40
        for i in range(len(closes)):
            self.assertEqual(strat.sma_crossover(closes, i, {}), "hold")

    def test_hold_when_insufficient_history(self):
        self.assertEqual(strat.sma_crossover([100.0, 101.0], 1, {}), "hold")


class TestRsiMeanReversion(unittest.TestCase):
    def test_buy_after_sustained_decline(self):
        closes = [100 - i for i in range(20)]
        self.assertEqual(strat.rsi_mean_reversion(closes, len(closes) - 1, {}), "buy")

    def test_sell_after_sustained_rally(self):
        closes = [100 + i for i in range(20)]
        self.assertEqual(strat.rsi_mean_reversion(closes, len(closes) - 1, {}), "sell")

    def test_hold_on_flat_series(self):
        closes = [100.0] * 20
        self.assertEqual(strat.rsi_mean_reversion(closes, len(closes) - 1, {}), "hold")


class TestVolatilityBreakout(unittest.TestCase):
    def test_hold_when_insufficient_history(self):
        self.assertEqual(strat.volatility_breakout([100.0] * 5, 4, {}), "hold")

    def test_hold_on_flat_series(self):
        closes = [100.0] * 30
        self.assertEqual(strat.volatility_breakout(closes, len(closes) - 1, {}), "hold")


class TestRegime(unittest.TestCase):
    def test_ranging_on_flat_series(self):
        closes = [100.0] * 40
        self.assertEqual(strat.regime(closes, len(closes) - 1), "ranging")

    def test_trending_on_strong_sustained_ramp(self):
        closes = [100.0] * 30 + [100 * (1.03 ** i) for i in range(1, 20)]
        self.assertEqual(strat.regime(closes, len(closes) - 1), "trending")

    def test_ranging_when_insufficient_history(self):
        self.assertEqual(strat.regime([100.0, 101.0], 1), "ranging")


class TestAdaptiveEnsemble(unittest.TestCase):
    def test_returns_a_valid_signal(self):
        closes = [100.0 + (i % 5) for i in range(50)]
        sig = strat.adaptive_ensemble(closes, len(closes) - 1, {})
        self.assertIn(sig, ("buy", "sell", "hold"))

    def test_hold_on_perfectly_flat_series(self):
        closes = [100.0] * 50
        self.assertEqual(strat.adaptive_ensemble(closes, len(closes) - 1, {}), "hold")

    def test_does_not_blindly_buy_a_real_downtrend(self):
        # a sustained decline is correctly read as a *trending* regime (verified
        # via strat.regime), where rsi_mean_reversion's lone "buy" (it always
        # reads a hard decline as oversold) is downweighted to zero -- the whole
        # point of adaptive_ensemble is not fighting an established trend the
        # way naive RSI mean-reversion alone would.
        closes = [100.0] * 20 + [100 - i for i in range(20)]
        self.assertEqual(strat.regime(closes, len(closes) - 1), "trending")
        self.assertEqual(strat.rsi_mean_reversion(closes, len(closes) - 1, {}), "buy")
        self.assertNotEqual(strat.adaptive_ensemble(closes, len(closes) - 1, {}), "buy")


class TestSTRATEGIESRegistry(unittest.TestCase):
    def test_all_registered_strategies_are_callable_and_return_valid_signals(self):
        closes = [100.0 + (i % 7) * 1.5 for i in range(60)]
        for name, fn in strat.STRATEGIES.items():
            with self.subTest(strategy=name):
                sig = fn(closes, len(closes) - 1, {})
                self.assertIn(sig, ("buy", "sell", "hold"))


if __name__ == "__main__":
    unittest.main()
