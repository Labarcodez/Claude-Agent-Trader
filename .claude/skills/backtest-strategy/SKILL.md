---
name: backtest-strategy
description: Fetch historical price data and backtest the trading strategies in backtest/strategies.py against it, producing a report on returns, drawdown, win rate, and Sharpe. Use before enabling a strategy for live autonomous trading, after changing risk.yaml or strategies.py, or whenever the user asks to backtest, evaluate, or validate a trading strategy.
---

# Backtest a strategy

1. Pick the CoinGecko coin id(s) to test (e.g. `solana`, `jupiter-exchange-solana`,
   `pyth-network`, `jito-governance-token`, `raydium`). Match these to the
   watchlist in `config/watchlist.yaml`.
2. Fetch history (skip if a recent cache file already exists in
   `backtest/cache/`):
   ```
   python3 backtest/fetch_history.py --coin <coin-id> --days 180
   ```
3. Run the comparison:
   ```
   python3 backtest/run_backtest.py --coin <coin-id> --days 180 --strategy all
   ```
4. Read the report critically, per `docs/STRATEGY.md` "Judging a backtest":
   - Total return alone is not enough -- check max drawdown (a strategy that
     makes 20% but draws down 40% along the way is not "better" for a $50
     account that has a circuit breaker at -60%) and win rate (a low win rate
     can still be profitable with a good risk/reward ratio, but combine with
     the trade log to sanity check).
   - Compare against buy-and-hold SOL over the same window as a baseline --
     a strategy that underperforms buy-and-hold after fees usually isn't
     worth the added complexity and tax/fee drag.
   - A strategy is only a candidate for live trading in
     `.claude/skills/trade-cycle/SKILL.md` step 4 if its backtest here is
     non-negative and the drawdown stays within what `circuit_breaker_floor_usd`
     in `config/risk.yaml` can absorb.
5. Save findings: the JSON report is auto-saved to `backtest/results/`. If
   this changes which strategy the live agent should prefer, update the
   guidance in `docs/STRATEGY.md`'s "Current strategy" section and say so
   explicitly to the user -- don't silently change live behavior.

Never treat a backtest as a guarantee. Markets regime-shift; re-run this
periodically (e.g. monthly, or after a stretch of live losses) rather than
trusting a single historical result indefinitely.
