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


STRATEGIES = {
    "sma_crossover": sma_crossover,
    "rsi_mean_reversion": rsi_mean_reversion,
    "volatility_breakout": volatility_breakout,
}
