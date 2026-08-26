# Trade journal

`journal/trades.jsonl` is the append-only log of every trade-cycle run: what
the agent observed, what it decided, why, and what actually happened on
Kraken. It is git-ignored by default (see `.gitignore`) because it's live
trading history tied to a real account, not project source -- but nothing
stops you from versioning it if you want a durable audit trail.

## Format

One JSON object per line:

```json
{
  "timestamp": "2026-08-18T18:30:00Z",
  "cycle_id": "c-000123",
  "portfolio_value_usd": 542.00,
  "positions_before": {"XBTUSD": 0.002, "USD": 120.0},
  "discovery": {
    "candidates_evaluated": 47,
    "eligible": 3,
    "rejected_sample": [{"altname": "XYZUSDT", "reason": "24h quote volume $80,000 < min $500,000"}]
  },
  "signals": [
    {"altname": "ETHUSD", "strategy": "adaptive_ensemble", "signal": "buy", "reason": "trending regime; fast SMA(10) crossed above slow SMA(30); volatility breakout confirmed"}
  ],
  "risk_checks": {
    "enabled": true,
    "circuit_breaker_tripped": false,
    "regime": "trending",
    "discovery_eligible": true,
    "tier": "blue_chip",
    "position_size_usd": 145.00,
    "portfolio_heat_pct": 0.08,
    "within_caps": true
  },
  "action": {
    "type": "order_buy",
    "altname": "ETHUSD",
    "order_type": "limit",
    "amount_usd": 145.00,
    "quote": {"expected_price": 3200.00, "spread_bps": 4, "orderbook_depth_usd": 500000},
    "executed": true,
    "kraken_txid": "…",
    "fill_price": 3201.50
  },
  "portfolio_value_usd_after": 541.10,
  "notes": "free-text rationale / anything unusual"
}
```

Every cycle should append an entry even when the decision is "hold" for every
token -- a gap in the log is a debugging blind spot. Use `"action": {"type": "hold"}`
for no-op cycles.

## `journal/paper_trades.jsonl` uses a different, simpler format

The format above is what `trade-cycle` (an LLM-driven skill) is instructed
to write. `paper_trading/run_paper_cycle.py` is a plain Python script, not
an LLM following instructions, so `journal/paper_trades.jsonl` has its own
fixed, code-defined shape instead -- don't expect the two files to match:

```json
{
  "timestamp": "2026-08-18T18:30:00Z",
  "type": "cycle",
  "portfolio_value_usd_before": 500.00,
  "portfolio_value_usd_after": 498.50,
  "regime_allows_new_entries": true,
  "discovery": {"evaluated": 42, "eligible": 6, "rejected": 36},
  "actions": [
    {"type": "buy", "symbol": "ETH", "altname": "ETHUSD", "tier": "blue_chip", "size_usd": 145.0, "fill_price": 3201.5, "signal_basis": "buy signal"},
    {"type": "sell", "symbol": "SOL", "altname": "SOLUSD", "reason": "stop-loss (-15.2%)", "return_pct": -15.2}
  ],
  "open_positions": 2
}
```

or, on a circuit breaker trip: `{"timestamp": ..., "type": "circuit_breaker_trip", "reason": "...", "portfolio_value_usd": 19.40}`.
See `paper_trading/run_paper_cycle.py`'s `append_journal()` call sites for
the authoritative shape if this drifts from the code -- unlike
`trades.jsonl`, this one's schema lives in code, not in an LLM's judgment,
so it's exact but less descriptive per entry.

## Why this matters

- It's the only way to sanity-check the strategy against what actually
  happened (backtests are never the same as live fills/slippage).
- It's what you re-read before raising `max_position_usd` or `enabled: true`
  after a circuit-breaker trip.
- If something ever looks wrong with the account balance, this is the first
  place to check what the agent thinks it did.
