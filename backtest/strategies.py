"""Simple, dependency-free trading strategies for backtest/engine.py.

Each strategy is a function: (closes: list[float], i: int, state: dict) -> signal
where signal is one of "buy", "sell", "hold". `i` is the index of the *current*
bar (the strategy may only look at closes[:i+1] -- no lookahead). `state` is a
per-run dict the strategy can use to remember things (e.g. crossover direction)
across calls; the engine creates a fresh dict per backtest run.

These are intentionally simple and are meant as a *starting point* for the
agent's real strategy, validated by backtesting before being trusted with the
live $50 account. See docs/STRATEGY.md for the reasoning behind each one and
for how to extend this file safely.
"""
from __future__ import annotations
import statistics as stats


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def rsi(values: list[float], window: int = 14) -> float | None:
    if len(values) < window + 1:
        return None
    gains, losses = [], []
    for j in range(len(values) - window, len(values)):
        delta = values[j] - values[j - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_gain == 0 and avg_loss == 0:
        return 50.0  # no price movement at all (e.g. a stale/illiquid feed) -- neutral, not "overbought"
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def sma_crossover(closes: list[float], i: int, state: dict, fast: int = 10, slow: int = 30) -> str:
    """Classic trend-following signal: buy when the fast SMA crosses above the
    slow SMA, sell when it crosses back below. Works best in trending markets,
    whipsaws in chop -- pair with the RSI filter strategy or a volume check
    before trusting it live."""
    window = closes[: i + 1]
    f, s = sma(window, fast), sma(window, slow)
    if f is None or s is None:
        return "hold"
    prev_f = sma(window[:-1], fast)
    prev_s = sma(window[:-1], slow)
    if prev_f is None or prev_s is None:
        return "hold"
    crossed_up = prev_f <= prev_s and f > s
    crossed_down = prev_f >= prev_s and f < s
    if crossed_up:
        return "buy"
    if crossed_down:
        return "sell"
    return "hold"


def rsi_mean_reversion(closes: list[float], i: int, state: dict, window: int = 14,
                        oversold: float = 30, overbought: float = 70) -> str:
    """Buy when RSI shows oversold conditions, sell when overbought. Works best
    range-bound / choppy markets; tends to fight strong trends, so it is a poor
    fit alone during a sustained breakout -- consider combining with a trend
    filter (e.g. only take RSI buy signals when price is above its 50-period SMA)."""
    window_vals = closes[: i + 1]
    r = rsi(window_vals, window)
    if r is None:
        return "hold"
    if r <= oversold:
        return "buy"
    if r >= overbought:
        return "sell"
    return "hold"


def rsi_mean_reversion_trend_filtered(closes: list[float], i: int, state: dict, window: int = 14,
                                       oversold: float = 30, overbought: float = 70, trend_sma: int = 50) -> str:
    """rsi_mean_reversion plus the trend filter its own docstring names as the
    fix for its main weakness: RSI can stay "oversold" for a long time during
    a genuine downtrend (a falling knife), not just during a range-bound dip.
    Only take the "buy" when price is also above its trend_sma-period SMA --
    i.e. only mean-revert within an established uptrend/range, not against a
    real breakdown. Sell signals are never filtered -- exiting a position
    must never wait on a trend confirmation that protecting capital doesn't
    need."""
    window_vals = closes[: i + 1]
    r = rsi(window_vals, window)
    if r is None:
        return "hold"
    if r >= overbought:
        return "sell"
    if r <= oversold:
        trend = sma(window_vals, trend_sma)
        if trend is not None and window_vals[-1] < trend:
            return "hold"  # oversold during a real downtrend -- don't catch the falling knife
        return "buy"
    return "hold"


def ema_series(values: list[float], window: int) -> list[float] | None:
    """Full exponential-moving-average series (not just the latest value) --
    MACD's signal line needs an EMA computed on top of another EMA's own
    output series, so the whole series has to be available, not just its
    final point. Seeded with a plain SMA of the first `window` values (the
    standard convention -- there's no prior EMA to seed from at the very
    start of a series). Returns None if there isn't even enough history to
    seed the first value."""
    if len(values) < window:
        return None
    multiplier = 2 / (window + 1)
    series = [sum(values[:window]) / window]
    for price in values[window:]:
        series.append((price - series[-1]) * multiplier + series[-1])
    return series


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float] | None:
    """Returns (macd_line, signal_line) as of the latest point in `values`.
    macd_line = fast EMA - slow EMA (a faster-reacting weighted average minus
    a slower one); signal_line is a signal-period EMA of the macd_line
    series itself, not of price -- the standard MACD construction. None if
    there isn't enough history yet (needs at least slow + signal bars)."""
    fast_ema = ema_series(values, fast)
    slow_ema = ema_series(values, slow)
    if fast_ema is None or slow_ema is None:
        return None
    # fast_ema starts `slow - fast` bars earlier than slow_ema (its window is
    # shorter, so it warms up sooner) -- align them to the same starting bar
    # before subtracting, or the two series would be comparing different points in time.
    offset = slow - fast
    if offset < 0 or len(fast_ema) <= offset:
        return None
    macd_line_series = [f - s for f, s in zip(fast_ema[offset:], slow_ema)]
    signal_series = ema_series(macd_line_series, signal)
    if signal_series is None:
        return None
    return macd_line_series[-1], signal_series[-1]


def macd_crossover(closes: list[float], i: int, state: dict, fast: int = 12, slow: int = 26, signal: int = 9) -> str:
    """Trend-following momentum signal, distinct from sma_crossover's plain
    moving-average cross: MACD's EMA-based construction weights recent
    prices more heavily than a flat-window SMA, so it tends to react sooner
    to a genuine trend change while still smoothing out single-bar noise.
    Buy when the MACD line crosses above its own signal line, sell on the
    reverse cross -- the classic MACD trading rule, using the standard
    12/26/9 windows by default."""
    window = closes[: i + 1]
    result = macd(window, fast, slow, signal)
    prev_result = macd(window[:-1], fast, slow, signal)
    if result is None or prev_result is None:
        return "hold"
    macd_line, signal_line = result
    prev_macd, prev_signal = prev_result
    if prev_macd <= prev_signal and macd_line > signal_line:
        return "buy"
    if prev_macd >= prev_signal and macd_line < signal_line:
        return "sell"
    return "hold"


def bollinger_mean_reversion(closes: list[float], i: int, state: dict, window: int = 20, num_std: float = 2.0) -> str:
    """Buy when price closes at or below its lower Bollinger Band (a
    window-period SMA minus num_std standard deviations of recent closes),
    sell at or above the upper band. Distinct from rsi_mean_reversion's
    bounded 0-100 oscillator: the bands widen and narrow with the asset's
    OWN recent volatility, so what counts as "oversold" adapts automatically
    to a calm vs. wild stretch instead of using the same fixed 30/70
    threshold regardless of how volatile the asset currently is -- a real
    difference for the wide range of volatility levels across Kraken's
    pairs (a calm blue-chip vs. a wild emerging-tier pair)."""
    window_vals = closes[: i + 1]
    if len(window_vals) < window:
        return "hold"
    recent = window_vals[-window:]
    mid = sum(recent) / window
    variance = sum((v - mid) ** 2 for v in recent) / window
    std = variance ** 0.5
    if std == 0:
        return "hold"  # flat/no-movement window -- no meaningful band to compare against
    price = window_vals[-1]
    if price <= mid - num_std * std:
        return "buy"
    if price >= mid + num_std * std:
        return "sell"
    return "hold"


def _stochastic_percent_k(closes: list[float], window: int) -> float | None:
    """Where the latest close sits within its trailing `window`-bar
    high-low range, as a percentage (0 = at the window's low, 100 = at its
    high). Uses CLOSING prices for the window's high/low -- this repo's
    strategy functions only ever see closes, not full OHLC, the same
    convention volatility_breakout's "recent high/low" already uses."""
    if len(closes) < window:
        return None
    recent = closes[-window:]
    lo, hi = min(recent), max(recent)
    if hi == lo:
        return 50.0  # flat window -- neither oversold nor overbought
    return (closes[-1] - lo) / (hi - lo) * 100


def stochastic_oscillator(closes: list[float], i: int, state: dict, k_window: int = 14, d_window: int = 3,
                          oversold: float = 20, overbought: float = 80) -> str:
    """The "slow stochastic" %D line (a d_window-period SMA of %K, the raw
    oscillator) crossing its oversold/overbought thresholds. Distinct from
    rsi_mean_reversion: RSI measures the size/speed of recent gains vs.
    losses, while %K measures WHERE price sits in its recent range --
    two different questions that can disagree (e.g. a slow grind to a
    range high reads high on %K without necessarily reading overbought on
    RSI's momentum measure). Smoothing over d_window bars (not just the raw
    %K) reduces single-bar noise, the same reason MACD smooths its own line
    with a signal-period EMA."""
    window = closes[: i + 1]
    k_values = [_stochastic_percent_k(window[: len(window) - j], k_window) for j in range(d_window)]
    if any(k is None for k in k_values):
        return "hold"
    d_value = sum(k_values) / len(k_values)
    if d_value <= oversold:
        return "buy"
    if d_value >= overbought:
        return "sell"
    return "hold"


def donchian_channel_breakout(closes: list[float], i: int, state: dict, window: int = 20) -> str:
    """Classic Donchian-channel ("turtle trading") breakout: buy on ANY new
    `window`-bar high, sell on any new `window`-bar low -- no confirmation
    beyond the extreme itself. Distinct from volatility_breakout, which
    requires clearing the recent high/low by a volatility-scaled buffer
    before firing: Donchian reacts to every new extreme (more trades, more
    false breakouts in chop), volatility_breakout only to ones that clear
    the noise floor (fewer trades, better-confirmed ones). Worth comparing
    directly against volatility_breakout's real (non-drift) backtest
    results -- see docs/STRATEGY.md "Trade frequency, not just trade
    quality" for why firing rate matters as much as the return number."""
    window_vals = closes[: i + 1]
    if len(window_vals) < window + 1:
        return "hold"
    prior = window_vals[-(window + 1):-1]
    price = window_vals[-1]
    if price > max(prior):
        return "buy"
    if price < min(prior):
        return "sell"
    return "hold"


def ema_ribbon(closes: list[float], i: int, state: dict, fast: int = 8, mid: int = 21, slow: int = 55) -> str:
    """Three-EMA trend-confirmation ribbon: fires only on the BAR WHERE the
    fast/mid/slow EMAs newly align bullishly (fast > mid > slow) or
    bearishly (fast < mid < slow) -- not on every bar the alignment holds,
    the same "fire on the transition" shape sma_crossover and
    macd_crossover already use. Requiring all three to agree makes this
    smoother/more selective than sma_crossover's single fast/slow cross, at
    the cost of confirming a reversal a bit later."""
    window = closes[: i + 1]
    fast_ema = ema_series(window, fast)
    mid_ema = ema_series(window, mid)
    slow_ema = ema_series(window, slow)
    if fast_ema is None or mid_ema is None or slow_ema is None:
        return "hold"
    bullish = fast_ema[-1] > mid_ema[-1] > slow_ema[-1]
    bearish = fast_ema[-1] < mid_ema[-1] < slow_ema[-1]

    prev_window = window[:-1]
    prev_fast = ema_series(prev_window, fast)
    prev_mid = ema_series(prev_window, mid)
    prev_slow = ema_series(prev_window, slow)
    if prev_fast is None or prev_mid is None or prev_slow is None:
        return "hold"  # not enough history yet to know whether this is a NEW alignment or a pre-existing one
    prev_bullish = prev_fast[-1] > prev_mid[-1] > prev_slow[-1]
    prev_bearish = prev_fast[-1] < prev_mid[-1] < prev_slow[-1]
    if bullish and not prev_bullish:
        return "buy"
    if bearish and not prev_bearish:
        return "sell"
    return "hold"


def volatility_breakout(closes: list[float], i: int, state: dict, lookback: int = 20,
                         breakout_mult: float = 1.0) -> str:
    """Buy when price breaks above the recent high by more than breakout_mult *
    stdev of returns (momentum continuation); sell on breakdown below the recent
    low by the same margin. Sensitive to lookback length -- backtest before
    tuning down."""
    window = closes[: i + 1]
    if len(window) < lookback + 2:
        return "hold"
    recent = window[-(lookback + 1):-1]
    hi, lo = max(recent), min(recent)
    rets = [(recent[j] - recent[j - 1]) / recent[j - 1] for j in range(1, len(recent))]
    vol = stats.pstdev(rets) if len(rets) > 1 else 0.0
    price = window[-1]
    threshold = vol * breakout_mult * price
    if price > hi + threshold:
        return "buy"
    if price < lo - threshold:
        return "sell"
    return "hold"


def realized_vol(values: list[float], window: int = 20) -> float | None:
    """Stdev of daily returns over the trailing window, as a fraction (e.g.
    0.03 = 3%/day). Used both for the regime filter's trend-strength check
    and for volatility-scaled position sizing (config/risk.yaml)."""
    if len(values) < window + 1:
        return None
    rets = [(values[j] - values[j - 1]) / values[j - 1] for j in range(len(values) - window, len(values))]
    return stats.pstdev(rets) if len(rets) > 1 else None


def regime(closes: list[float], i: int, fast: int = 10, slow: int = 30, vol_window: int = 20) -> str:
    """Classifies the current bar as "trending" or "ranging" using trend
    strength (how far apart the fast/slow SMAs are) relative to recent
    volatility. Not a tradable signal on its own -- used by adaptive_ensemble
    and documented in config/risk.yaml's regime_filter as the same idea
    applied at the whole-market level (BTC vs. its own SMA) to gate new
    entries. A wide fast/slow SMA gap relative to volatility means a real
    trend is underway; a narrow gap means the market is chopping sideways."""
    window = closes[: i + 1]
    f, s = sma(window, fast), sma(window, slow)
    vol = realized_vol(window, vol_window)
    if f is None or s is None or vol is None or vol == 0 or window[-1] == 0:
        return "ranging"  # default to the more conservative regime when data is thin
    trend_strength = abs(f - s) / (window[-1] * vol)
    return "trending" if trend_strength > 1.5 else "ranging"


def _adaptive_ensemble_core(closes: list[float], i: int, state: dict, fast: int = 10, slow: int = 30,
                             rsi_window: int = 14, vol_lookback: int = 20) -> str:
    """Shared logic behind adaptive_ensemble and adaptive_ensemble_fast: a
    regime-aware weighted vote across the three base strategies, instead of
    picking one strategy and hoping the market cooperates. In a trending
    regime, trend-following signals (SMA crossover, volatility breakout) get
    most of the weight; in a ranging regime, mean-reversion (RSI) dominates.
    A signal only fires if the weighted vote clears +/-0.5 -- disagreement
    between sub-strategies resolves to "hold" rather than guessing."""
    r = regime(closes, i, fast=fast, slow=slow)
    sub_signals = {
        "sma_crossover": sma_crossover(closes, i, state, fast=fast, slow=slow),
        "rsi_mean_reversion": rsi_mean_reversion(closes, i, state, window=rsi_window),
        "volatility_breakout": volatility_breakout(closes, i, state, lookback=vol_lookback),
    }
    weights = (
        {"sma_crossover": 0.55, "volatility_breakout": 0.45, "rsi_mean_reversion": 0.0}
        if r == "trending"
        else {"rsi_mean_reversion": 0.7, "sma_crossover": 0.15, "volatility_breakout": 0.15}
    )
    score = 0.0
    for name, sig in sub_signals.items():
        vote = {"buy": 1, "sell": -1, "hold": 0}[sig]
        score += vote * weights[name]
    if score >= 0.5:
        return "buy"
    if score <= -0.5:
        return "sell"
    return "hold"


def adaptive_ensemble(closes: list[float], i: int, state: dict) -> str:
    """Regime-aware weighted vote across the three base strategies, using the
    original 10/30-bar SMA, 14-bar RSI, 20-bar breakout windows.

    This is the strategy config/risk.yaml and the trade-cycle skill expect to
    be the default live strategy once it backtests acceptably (see
    docs/STRATEGY.md "Judging a backtest" and the --walk-forward flag on
    backtest/run_backtest.py) -- but it is not a guarantee, and it should be
    re-validated the same as any other strategy before being trusted, and
    periodically after."""
    return _adaptive_ensemble_core(closes, i, state, fast=10, slow=30, rsi_window=14, vol_lookback=20)


def adaptive_ensemble_fast(closes: list[float], i: int, state: dict) -> str:
    """Same regime-aware ensemble as adaptive_ensemble, but with roughly half
    the lookback (5/15-bar SMA, 7-bar RSI, 10-bar breakout).

    The hypothesis this tests: adaptive_ensemble's default windows were sized
    for slower-moving assets like SOL/BTC, and may react too slowly for the
    short-lived, high-volatility price action typical of emerging-tier /
    memecoin candidates, where a real trend or reversal can complete in days,
    not weeks. This is a hypothesis to backtest, not an assumed improvement --
    a shorter window also means more false signals in genuine chop, so it is
    a real trade-off. Only trust whichever of the two variants the walk-forward
    comparison actually favors on a given asset class; do not prefer this one
    by default just because it reacts faster."""
    return _adaptive_ensemble_core(closes, i, state, fast=5, slow=15, rsi_window=7, vol_lookback=10)


STRATEGIES = {
    "sma_crossover": sma_crossover,
    "rsi_mean_reversion": rsi_mean_reversion,
    "rsi_mean_reversion_trend_filtered": rsi_mean_reversion_trend_filtered,
    "volatility_breakout": volatility_breakout,
    "macd_crossover": macd_crossover,
    "bollinger_mean_reversion": bollinger_mean_reversion,
    "stochastic_oscillator": stochastic_oscillator,
    "donchian_channel_breakout": donchian_channel_breakout,
    "ema_ribbon": ema_ribbon,
    "adaptive_ensemble": adaptive_ensemble,
    "adaptive_ensemble_fast": adaptive_ensemble_fast,
}
