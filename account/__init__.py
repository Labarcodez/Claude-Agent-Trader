"""Deterministic account math: portfolio valuation, fee-aware cost estimation,
and order-precision safety for the Kraken-based trading agent.

Why this package exists: the trade-cycle skill is an LLM following
instructions, and "how much is actually in the account" / "what will this
trade cost in fees" are exactly the kind of arithmetic an LLM can get subtly
wrong under time/context pressure -- a wrong portfolio total silently
corrupts every risk check downstream (position sizing, circuit breaker,
portfolio heat), and a wrong fee assumption can size a trade that never
clears its own costs. This package turns that arithmetic into small, pure,
unit-tested Python functions the skill calls instead of computing by hand.
See docs/STRATEGY.md "Precise portfolio valuation & fee-aware sizing".

Modules:
- kraken_common -- shared Kraken asset-code normalization + a best-effort
  kraken-cli subprocess wrapper (not the primary live-trading integration
  path -- see its docstring).
- portfolio -- normalize raw balances, compute USD valuation with an
  explicit, conservative (excluded, not zero-defaulted) handling of any
  asset that couldn't be priced.
- fees -- parse Kraken's live fee-tier data, compute round-trip cost, and
  check whether a position's take-profit target actually clears its own
  fees by a healthy margin.
- precision -- round order price/volume to a pair's required precision, and
  check a proposed size against the pair's own Kraken-enforced minimums.
"""
