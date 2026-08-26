---
name: backtest-strategy
description: Fetch historical price data and backtest the trading strategies in backtest/strategies.py against it (including an out-of-sample walk-forward check), producing a report on returns, drawdown, win rate, Sharpe, and overfit risk. Covers both a single Kraken pair and a comprehensive "backtest everything currently eligible" run. Use before enabling a strategy for live autonomous trading, after changing risk.yaml or strategies.py, or whenever the user asks to backtest, evaluate, or validate a trading strategy.
---

# Backtest a strategy

## Comprehensive: backtest everything currently eligible

For a full picture rather than one pair at a time, run:
```
python3 backtest/backtest_all.py
```
This runs discovery itself, then walk-forward backtests every eligible
candidate (plus BTC and ETH always) against every strategy in one pass,
printing a ranked summary table and saving the full report to
`backtest/results/backtest_all_*.json`. Use `--max-candidates` to widen or
narrow the discovery pool, `--skip-discovery` for a fast BTC/ETH-only sanity
check, and the same `--min-24h-quote-volume-usd` / `--max-spread-bps` / etc.
flags as `research/discover_candidates.py` to explore how a looser or
tighter universe would have backtested (this never changes what actually
trades live -- that's still `config/discovery.yaml`). Prefer this over
the single-pair workflow below whenever the question is "how is the
strategy doing across the current universe," not just "how does it do on
this one pair" -- it's also the right tool for periodically re-checking
`adaptive_ensemble`'s health, per "Judging a backtest" below.

Read `docs/STRATEGY.md` "Autonomous discovery" for what this tends to
surface: emerging-tier pairs often show *both* larger returns and larger
drawdowns than BTC/ETH in the same run -- live evidence for why
`config/discovery.yaml`'s tier sizing exists, not just a theoretical
justification.

## Single pair

1. Pick the Kraken pair altname(s) to test (e.g. `XBTUSD`, `ETHUSD`,
   `SOLUSD`) -- there's no fixed list (see `research/discover_candidates.py`)
   -- use whatever's currently eligible in the latest
   `research/results/discovery_*.json`, plus `XBTUSD` always -- it's the
   regime-filter reference pair (`config/risk.yaml`'s `regime_reference_pair`),
   so it's worth knowing how the filter would have behaved over the same
   window you're backtesting.
2. Fetch history (skip if a recent cache file already exists in
   `backtest/cache/`):
   ```
   python3 backtest/fetch_history.py --pair XBTUSD --days 180
   python3 backtest/fetch_history.py --coin bitcoin --days 365   # CoinGecko fallback for a longer window than Kraken's 720-candle cap
   ```
3. Run the **walk-forward** comparison (preferred -- catches overfitting):
   ```
   python3 backtest/run_backtest.py --coin XBTUSD --days 180 --strategy all --walk-forward
   ```
   (`run_backtest.py`'s `--coin` flag is really "cache key" -- pass whatever
   key you used with `fetch_history.py`, Kraken pair altname or CoinGecko
   coin id alike.) A plain in-sample run (no `--walk-forward`) is fine for a
   first look, but don't trust it alone before going live -- see step 4.
4. Read the report critically, per `docs/STRATEGY.md` "Judging a backtest":
   - **Overfit risk**: the CLI already flags a large train/test gap. Treat
     `HIGH` as disqualifying for live use until investigated -- don't just
     pick the strategy with the best in-sample number.
   - Total return alone is not enough -- check max drawdown (can
     `circuit_breaker_floor_usd` in `config/risk.yaml` absorb it?) and win
     rate (a low win rate can still be profitable with good risk/reward, but
     cross-check with the trade log).
   - Compare against simple buy-and-hold over the same window. Complexity
     has to earn its keep.
   - `adaptive_ensemble` (regime-aware weighted vote across the other three)
     is the default candidate for live use once it clears the bar above --
     but re-validate it the same as any other strategy, not on faith.
5. A strategy is only a candidate for live trading in
   `.claude/skills/trade-cycle/SKILL.md` step 6 if its **out-of-sample**
   result is non-negative, overfit risk isn't flagged HIGH, and the drawdown
   fits within what the circuit breaker can absorb.
6. Save findings: the JSON report auto-saves to `backtest/results/`. If this
   changes which strategy the live agent should prefer, update
   `docs/STRATEGY.md`'s "Current strategy" section and say so explicitly to
   the user -- don't silently change live behavior.

Never treat a backtest as a guarantee, walk-forward or not. Markets
regime-shift; re-run this periodically (monthly, or after a stretch of live
losses) rather than trusting a single historical result indefinitely.
Backtests here charge Kraken's own fee/slippage assumptions
(`backtest/engine.py`'s `fee_bps`/`slippage_bps` defaults, see
docs/STRATEGY.md "Fee-aware execution") -- if you change your account's real
fee tier materially, re-run with `--fee-bps` adjusted accordingly rather
than trusting the shipped default forever.
