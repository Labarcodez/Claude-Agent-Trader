"""Minimal single-asset backtesting engine, stdlib only.

Simulates trading a strategy against a cached CoinGecko price series, applying
a fee/slippage cost per trade and simple position sizing (all-in / all-out on
buy/sell signals -- good enough for evaluating a signal's edge before it's
allowed to touch the live $50 account; the live agent's *actual* position
sizing/risk rules live in config/risk.yaml and are enforced separately by the
trade-cycle skill, not by this engine).
"""
from __future__ import annotations
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"


@dataclass
class Trade:
    side: str          # "buy" or "sell"
    index: int
    price: float
    timestamp_ms: int


@dataclass
class BacktestResult:
    strategy: str
    coin: str
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)   # list of (timestamp_ms, equity)
    starting_equity: float = 1.0
    ending_equity: float = 1.0

    @property
    def total_return_pct(self) -> float:
        return (self.ending_equity / self.starting_equity - 1) * 100

    @property
    def num_round_trips(self) -> int:
        return len([t for t in self.trades if t.side == "sell"])

    @property
    def win_rate_pct(self) -> float | None:
        wins, total = 0, 0
        entry_price = None
        for t in self.trades:
            if t.side == "buy":
                entry_price = t.price
            elif t.side == "sell" and entry_price is not None:
                total += 1
                if t.price > entry_price:
                    wins += 1
                entry_price = None
        return (wins / total * 100) if total else None

    @property
    def max_drawdown_pct(self) -> float:
        peak = -math.inf
        max_dd = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                dd = (equity - peak) / peak
                max_dd = min(max_dd, dd)
        return max_dd * 100

    @property
    def sharpe_approx(self) -> float | None:
        """Rough daily-return Sharpe (not annualized properly for sub-daily data;
        treat as a relative ranking signal between strategies, not an absolute
        number)."""
        rets = []
        for j in range(1, len(self.equity_curve)):
            prev = self.equity_curve[j - 1][1]
            cur = self.equity_curve[j][1]
            if prev > 0:
                rets.append((cur - prev) / prev)
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        if sd == 0:
            return None
        return (mean / sd) * math.sqrt(365)


def load_cached_prices(coin_id: str, days: int) -> list[tuple[int, float]]:
    path = CACHE_DIR / f"{coin_id}_{days}d.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No cached data at {path}. Run: python3 backtest/fetch_history.py --coin {coin_id} --days {days}"
        )
    payload = json.loads(path.read_text())
    return [(int(ts), float(price)) for ts, price in payload["prices"]]


def run_backtest(coin_id: str, days: int, strategy_fn, fee_bps: float = 30,
                  slippage_bps: float = 50, **strategy_kwargs) -> BacktestResult:
    """fee_bps + slippage_bps model realistic Solana swap costs (~0.3% fee is
    high vs. Jupiter's actual fee-free routing via Phantom, but conservative is
    safer than optimistic when deciding whether a strategy has real edge)."""
    series = load_cached_prices(coin_id, days)
    closes = [p for _, p in series]
    cost_frac = (fee_bps + slippage_bps) / 10_000

    result = BacktestResult(strategy=strategy_fn.__name__, coin=coin_id)
    state: dict = {}
    cash = 1.0
    position = 0.0  # units of the asset, in "cash-equivalent at entry" terms simplified to fraction
    holding = False
    entry_price = None

    for i, (ts, price) in enumerate(series):
        signal = strategy_fn(closes, i, state, **strategy_kwargs)

        if signal == "buy" and not holding:
            cash *= (1 - cost_frac)
            holding = True
            entry_price = price
            result.trades.append(Trade("buy", i, price, ts))
        elif signal == "sell" and holding:
            ret = (price / entry_price) - 1
            cash *= (1 + ret)
            cash *= (1 - cost_frac)
            holding = False
            entry_price = None
            result.trades.append(Trade("sell", i, price, ts))

        # mark-to-market equity for the curve/drawdown calc
        if holding and entry_price:
            equity = cash * (price / entry_price)
        else:
            equity = cash
        result.equity_curve.append((ts, equity))

    # close any open position at the final price for reporting purposes
    if holding and entry_price:
        final_price = closes[-1]
        ret = (final_price / entry_price) - 1
        cash *= (1 + ret) * (1 - cost_frac)

    result.ending_equity = cash
    return result


def format_report(result: BacktestResult) -> str:
    lines = [
        f"Strategy:        {result.strategy}",
        f"Coin:            {result.coin}",
        f"Total return:    {result.total_return_pct:+.2f}%",
        f"Round trips:     {result.num_round_trips}",
        f"Win rate:        {result.win_rate_pct:.1f}%" if result.win_rate_pct is not None else "Win rate:        n/a (no closed trades)",
        f"Max drawdown:    {result.max_drawdown_pct:.2f}%",
        f"Sharpe (approx): {result.sharpe_approx:.2f}" if result.sharpe_approx is not None else "Sharpe (approx): n/a",
    ]
    return "\n".join(lines)
