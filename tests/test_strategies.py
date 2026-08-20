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


class TestRsiMeanReversionTrendFiltered(unittest.TestCase):
    def test_buy_after_dip_within_an_uptrend(self):
        # a strong 60-bar ramp, then a choppy-but-net-negative 14-bar tail
        # (more/bigger down days than up days) -- oversold enough for RSI to
        # fire, but price stays well above its 50-SMA since the ramp anchors
        # it high. This is "oversold within an uptrend", the case the filter
        # is meant to still allow (unlike test_does_not_buy_a_real_downtrend).
        closes = [100 + i * 3 for i in range(60)]
        p = closes[-1]
        for d in [1, -3, 1, -3, 1, -3, 1, -3, 1, -3, 1, -3, 1, -3]:
            p += d
            closes.append(p)
        i = len(closes) - 1
        self.assertLessEqual(strat.rsi(closes[:i + 1], 14), 30)
        self.assertGreater(closes[-1], strat.sma(closes[:i + 1], 50))
        self.assertEqual(strat.rsi_mean_reversion_trend_filtered(closes, i, {}), "buy")

    def test_does_not_buy_a_real_downtrend(self):
        # a long, real decline -- RSI reads oversold, but price is below its
        # own 50-SMA the whole way down, so the trend filter must block the buy
        # rsi_mean_reversion (unfiltered) would take.
        closes = [100 - i for i in range(60)]
        self.assertEqual(strat.rsi_mean_reversion(closes, len(closes) - 1, {}), "buy")
        self.assertEqual(strat.rsi_mean_reversion_trend_filtered(closes, len(closes) - 1, {}), "hold")

    def test_sell_after_sustained_rally_is_never_filtered(self):
        closes = [100 + i for i in range(20)]
        self.assertEqual(strat.rsi_mean_reversion_trend_filtered(closes, len(closes) - 1, {}), "sell")

    def test_hold_on_flat_series(self):
        closes = [100.0] * 60
        self.assertEqual(strat.rsi_mean_reversion_trend_filtered(closes, len(closes) - 1, {}), "hold")


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


class TestAdaptiveEnsembleFast(unittest.TestCase):
    """adaptive_ensemble_fast shares adaptive_ensemble's logic with roughly
    half the lookback windows -- same behavioral guarantees should hold, just
    reacting sooner (see backtest/strategies.py's docstring for the
    short-lived/high-volatility-asset hypothesis this variant tests)."""

    def test_returns_a_valid_signal(self):
        closes = [100.0 + (i % 5) for i in range(50)]
        sig = strat.adaptive_ensemble_fast(closes, len(closes) - 1, {})
        self.assertIn(sig, ("buy", "sell", "hold"))

    def test_hold_on_perfectly_flat_series(self):
        closes = [100.0] * 50
        self.assertEqual(strat.adaptive_ensemble_fast(closes, len(closes) - 1, {}), "hold")

    def test_does_not_blindly_buy_a_real_downtrend(self):
        closes = [100.0] * 20 + [100 - i for i in range(20)]
        self.assertNotEqual(strat.adaptive_ensemble_fast(closes, len(closes) - 1, {}), "buy")

    def test_reacts_at_least_as_fast_as_the_original_on_a_sharp_reversal(self):
        # A short decline followed by a sharp, sustained recovery -- shorter
        # windows should recognize the new uptrend no later than the original
        # 10/30 windows do, since that's the entire point of this variant.
        closes = [100.0 - i for i in range(15)] + [85.0 + i * 2 for i in range(20)]
        fast_signals = [strat.adaptive_ensemble_fast(closes, i, {}) for i in range(len(closes))]
        original_signals = [strat.adaptive_ensemble(closes, i, {}) for i in range(len(closes))]
        first_fast_buy = next((i for i, s in enumerate(fast_signals) if s == "buy"), None)
        first_original_buy = next((i for i, s in enumerate(original_signals) if s == "buy"), None)
        self.assertIsNotNone(first_fast_buy, "fast variant never bought the recovery")
        if first_original_buy is not None:
            self.assertLessEqual(first_fast_buy, first_original_buy)


class TestSTRATEGIESRegistry(unittest.TestCase):
    def test_all_registered_strategies_are_callable_and_return_valid_signals(self):
        closes = [100.0 + (i % 7) * 1.5 for i in range(60)]
        for name, fn in strat.STRATEGIES.items():
            with self.subTest(strategy=name):
                sig = fn(closes, len(closes) - 1, {})
                self.assertIn(sig, ("buy", "sell", "hold"))


if __name__ == "__main__":
    unittest.main()
