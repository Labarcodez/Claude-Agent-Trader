# Trade journal

`journal/trades.jsonl` is the append-only log of every trade-cycle run: what
the agent observed, what it decided, why, and what actually happened on-chain.
It is git-ignored by default (see `.gitignore`) because it's live trading
history tied to a real wallet, not project source -- but nothing stops you
from versioning it if you want a durable audit trail.

## Format

One JSON object per line:

```json
{
  "timestamp": "2026-08-18T18:30:00Z",
  "cycle_id": "c-000123",
  "portfolio_value_usd": 54.20,
  "positions_before": {"SOL": 0.31, "USDC": 12.0},
  "discovery": {
    "candidates_found": 47,
    "eligible": 3,
    "rejected_sample": [{"symbol": "USDT", "reason": "mint authority not disabled"}]
  },
  "signals": [
    {"symbol": "JUP", "strategy": "adaptive_ensemble", "signal": "buy", "reason": "trending regime; fast SMA(10) crossed above slow SMA(30); volatility breakout confirmed"}
  ],
  "risk_checks": {
    "enabled": true,
    "circuit_breaker_tripped": false,
    "regime": "trending",
    "discovery_eligible": true,
    "tier": "blue_chip",
    "position_size_usd": 14.50,
    "portfolio_heat_pct": 0.08,
    "within_caps": true
  },
  "action": {
    "type": "buy_token",
    "symbol": "JUP",
    "amount_usd": 14.50,
    "quote": {"expected_price": 0.42, "slippage_bps": 45, "price_impact_bps": 30},
    "executed": true,
    "tx_signature": "…",
    "fill_price": 0.421
  },
  "portfolio_value_usd_after": 54.10,
  "notes": "free-text rationale / anything unusual"
}
```

Every cycle should append an entry even when the decision is "hold" for every
token -- a gap in the log is a debugging blind spot. Use `"action": {"type": "hold"}`
for no-op cycles.

## Why this matters

- It's the only way to sanity-check the strategy against what actually
  happened (backtests are never the same as live fills/slippage).
- It's what you re-read before raising `max_position_usd` or `enabled: true`
  after a circuit-breaker trip.
- If something ever looks wrong with the wallet balance, this is the first
  place to check what the agent thinks it did.
