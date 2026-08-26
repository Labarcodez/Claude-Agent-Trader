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


class TestStochasticPercentK(unittest.TestCase):
    def test_none_when_insufficient_data(self):
        self.assertIsNone(strat._stochastic_percent_k([1.0, 2.0], 5))

    def test_at_the_window_high_reads_100(self):
        self.assertAlmostEqual(strat._stochastic_percent_k([1.0, 2.0, 3.0, 10.0], 4), 100.0)

    def test_at_the_window_low_reads_0(self):
        self.assertAlmostEqual(strat._stochastic_percent_k([10.0, 3.0, 2.0, 1.0], 4), 0.0)

    def test_flat_window_reads_50_not_a_crash(self):
        self.assertAlmostEqual(strat._stochastic_percent_k([5.0, 5.0, 5.0], 3), 50.0)


class TestStochasticOscillator(unittest.TestCase):
    def test_hold_when_insufficient_history(self):
        self.assertEqual(strat.stochastic_oscillator([100.0] * 5, 4, {}, k_window=14, d_window=3), "hold")

    def test_buy_after_sustained_decline_to_the_range_low(self):
        closes = [100.0] * 10 + [100 - i for i in range(20)]
        self.assertEqual(strat.stochastic_oscillator(closes, len(closes) - 1, {}, k_window=14, d_window=3), "buy")

    def test_sell_after_sustained_rally_to_the_range_high(self):
        closes = [100.0] * 10 + [100 + i for i in range(20)]
        self.assertEqual(strat.stochastic_oscillator(closes, len(closes) - 1, {}, k_window=14, d_window=3), "sell")

    def test_hold_in_the_middle_of_the_range(self):
        closes = [100.0, 105.0, 95.0, 102.0, 98.0] * 5
        self.assertEqual(strat.stochastic_oscillator(closes, len(closes) - 1, {}, k_window=14, d_window=3), "hold")


class TestDonchianChannelBreakout(unittest.TestCase):
    def test_hold_when_insufficient_history(self):
        self.assertEqual(strat.donchian_channel_breakout([100.0] * 5, 4, {}, window=20), "hold")

    def test_hold_on_flat_series(self):
        closes = [100.0] * 25
        self.assertEqual(strat.donchian_channel_breakout(closes, len(closes) - 1, {}, window=20), "hold")

    def test_buy_on_a_new_window_high(self):
        closes = [100.0] * 20 + [105.0]  # a fresh high above the entire prior 20-bar window
        self.assertEqual(strat.donchian_channel_breakout(closes, len(closes) - 1, {}, window=20), "buy")

    def test_sell_on_a_new_window_low(self):
        closes = [100.0] * 20 + [95.0]
        self.assertEqual(strat.donchian_channel_breakout(closes, len(closes) - 1, {}, window=20), "sell")

    def test_fires_on_any_new_extreme_no_confirmation_buffer(self):
        # unlike volatility_breakout, the tiniest new high must fire --
        # no volatility-scaled buffer to clear first
        closes = [100.0] * 20 + [100.001]
        self.assertEqual(strat.donchian_channel_breakout(closes, len(closes) - 1, {}, window=20), "buy")


class TestEmaRibbon(unittest.TestCase):
    def test_hold_when_insufficient_history(self):
        self.assertEqual(strat.ema_ribbon([100.0] * 10, 9, {}, fast=8, mid=21, slow=55), "hold")

    def test_hold_on_perfectly_flat_series(self):
        closes = [100.0] * 120
        for i in range(len(closes)):
            self.assertEqual(strat.ema_ribbon(closes, i, {}, fast=8, mid=21, slow=55), "hold")

    def test_buy_appears_on_a_sustained_ramp(self):
        closes = [100.0] * 60 + [100 + i * 2 for i in range(60)]
        signals = [strat.ema_ribbon(closes, i, {}, fast=8, mid=21, slow=55) for i in range(len(closes))]
        self.assertIn("buy", signals)

    def test_sell_appears_on_a_sustained_decline(self):
        closes = [100.0] * 60 + [100 - i for i in range(60)]
        signals = [strat.ema_ribbon(closes, i, {}, fast=8, mid=21, slow=55) for i in range(len(closes))]
        self.assertIn("sell", signals)

    def test_only_fires_on_the_transition_not_every_bar_while_aligned(self):
        # a long-established ramp: the bullish alignment already existed
        # bars ago and should not keep re-firing "buy" every subsequent bar
        closes = [100.0] * 60 + [100 + i * 2 for i in range(80)]
        signals = [strat.ema_ribbon(closes, i, {}, fast=8, mid=21, slow=55) for i in range(len(closes))]
        self.assertLess(signals.count("buy"), 5, "should fire once around the transition, not on most of the ramp")


class TestVolatilityBreakout(unittest.TestCase):
    def test_hold_when_insufficient_history(self):
        self.assertEqual(strat.volatility_breakout([100.0] * 5, 4, {}), "hold")

    def test_hold_on_flat_series(self):
        closes = [100.0] * 30
        self.assertEqual(strat.volatility_breakout(closes, len(closes) - 1, {}), "hold")


class TestEmaSeries(unittest.TestCase):
    def test_none_when_insufficient_data(self):
        self.assertIsNone(strat.ema_series([1.0, 2.0], 5))

    def test_seeds_first_value_with_sma(self):
        series = strat.ema_series([1.0, 2.0, 3.0], 3)
        self.assertAlmostEqual(series[0], 2.0)

    def test_series_length_matches_input_minus_seed_window_plus_one(self):
        values = [float(i) for i in range(10)]
        series = strat.ema_series(values, 3)
        self.assertEqual(len(series), len(values) - 3 + 1)

    def test_tracks_a_steady_ramp_upward(self):
        values = [float(i) for i in range(20)]
        series = strat.ema_series(values, 5)
        self.assertGreater(series[-1], series[0])


class TestMacd(unittest.TestCase):
    def test_none_when_insufficient_data(self):
        self.assertIsNone(strat.macd([1.0] * 10, fast=12, slow=26, signal=9))

    def test_flat_series_has_zero_macd_line(self):
        result = strat.macd([100.0] * 60)
        self.assertIsNotNone(result)
        macd_line, signal_line = result
        self.assertAlmostEqual(macd_line, 0.0)
        self.assertAlmostEqual(signal_line, 0.0)

    def test_sustained_uptrend_has_positive_macd_line(self):
        closes = [100.0 + i for i in range(60)]
        macd_line, _ = strat.macd(closes)
        self.assertGreater(macd_line, 0)

    def test_sustained_downtrend_has_negative_macd_line(self):
        closes = [200.0 - i for i in range(60)]
        macd_line, _ = strat.macd(closes)
        self.assertLess(macd_line, 0)


class TestMacdCrossover(unittest.TestCase):
    def test_hold_when_insufficient_history(self):
        self.assertEqual(strat.macd_crossover([100.0] * 10, 9, {}), "hold")

    def test_hold_on_perfectly_flat_series(self):
        closes = [100.0] * 60
        for i in range(len(closes)):
            self.assertEqual(strat.macd_crossover(closes, i, {}), "hold")

    def test_buy_appears_on_a_sharp_sustained_reversal_upward(self):
        # flat, then a real declining stretch, then a sharp sustained ramp --
        # the MACD line must eventually cross back above its signal line
        closes = [100.0] * 30 + [100 - i for i in range(20)] + [80 + i * 4 for i in range(20)]
        signals = [strat.macd_crossover(closes, i, {}) for i in range(len(closes))]
        self.assertIn("buy", signals)


class TestBollingerMeanReversion(unittest.TestCase):
    def test_hold_when_insufficient_history(self):
        self.assertEqual(strat.bollinger_mean_reversion([100.0] * 5, 4, {}, window=20), "hold")

    def test_hold_on_perfectly_flat_series(self):
        # zero stdev -- no meaningful band, must not divide by zero or misfire
        closes = [100.0] * 25
        self.assertEqual(strat.bollinger_mean_reversion(closes, len(closes) - 1, {}), "hold")

    def test_buy_when_price_drops_below_lower_band(self):
        # a calm, tight range, then one sharp single-bar drop far below it --
        # the drop bar itself should read as oversold relative to the tight band
        closes = [100.0, 100.5, 99.5, 100.2, 99.8] * 4 + [80.0]
        self.assertEqual(strat.bollinger_mean_reversion(closes, len(closes) - 1, {}, window=20), "buy")

    def test_sell_when_price_spikes_above_upper_band(self):
        closes = [100.0, 100.5, 99.5, 100.2, 99.8] * 4 + [120.0]
        self.assertEqual(strat.bollinger_mean_reversion(closes, len(closes) - 1, {}, window=20), "sell")

    def test_hold_within_the_bands(self):
        closes = [100.0, 100.5, 99.5, 100.2, 99.8] * 4 + [100.1]
        self.assertEqual(strat.bollinger_mean_reversion(closes, len(closes) - 1, {}, window=20), "hold")


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
