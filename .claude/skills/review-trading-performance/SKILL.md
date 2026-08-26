---
name: review-trading-performance
description: Analyze accumulated paper (or live) trading history to find real, evidence-backed patterns -- which strategies/tiers/pairs are actually winning or losing, which exit reasons dominate, which discovery thresholds are chronically blocking trades -- and turn a genuine pattern into a specific, human-reviewed proposed change. This is how the system improves over time without silently drifting. Use when the user asks to review performance, "learn from" the trading history, check how paper trading is going, or periodically (e.g. weekly, or every ~50-100 cycles) as standing maintenance.
---

# Review trading performance

This is the feedback-loop half of the pipeline: `backtest-strategy` validates
a strategy *before* it trades; this skill looks at what actually happened
*after* it did, on real (paper or live) fills, and turns that into
improvement -- without ever silently rewriting `config/risk.yaml` or
`config/discovery.yaml` on its own. Every proposed change here needs the
same human sign-off any other config change does (CLAUDE.md rule 7): this
skill's job is to make that decision well-evidenced, not to make it
unsupervised.

## 1. Run the analysis

```
python3 scripts/analyze_journal.py                          # paper journal (default)
python3 scripts/analyze_journal.py --journal journal/trades.jsonl   # once real trades exist
```

This reports, from every closed trade in the journal: win rate and average
return broken down by exit reason, tier, strategy, and pair, plus the most
common reasons an eligible candidate was passed over. It describes what
happened -- it does not recommend anything on its own; that's this skill's
job, done with more context than the script has.

**Fewer than 10 closed trades total, or fewer than 10 in a specific
breakdown row**: say so plainly and stop there for that row. A pattern from
3 trades is noise, not evidence -- the exact same statistical-significance
caution `docs/STRATEGY.md`'s "Judging a backtest" applies to a short
backtest window applies here, arguably more so (live/paper fills are
noisier than a clean backtest). Do not propose a change on a small sample
just because the script printed a number.

## 2. Read the patterns that DO have enough data, critically

For each breakdown row with >= 10 trades, ask what it's actually evidence
of before proposing anything:

- **A tier or pair with a much worse win rate/avg return than others**:
  is this a strategy problem (wrong signal for this tier's volatility), a
  sizing problem (position too large for the tier's real risk), or just
  this tier's expected variance playing out (emerging-tier pairs are
  *supposed* to have worse average outcomes than blue-chip -- that's the
  whole reason tier multipliers exist, see `docs/STRATEGY.md` "Risk
  tiers"). Only the first two are things to act on.
- **A strategy underperforming `adaptive_ensemble` on real fills**: cross-
  check against its own backtest result first (`backtest/backtest_all.py`
  results, or docs/STRATEGY.md's "Current strategies" section). If the
  backtest already looked weak (like `bollinger_mean_reversion`'s
  documented result), this just confirms it -- not a new finding. If the
  backtest looked strong but real fills don't confirm it (like
  `macd_crossover`'s backtest-vs-reality gap documented in
  `docs/STRATEGY.md`), that's the important case: the backtest was
  measuring something (mark-to-market drift) that doesn't survive contact
  with actually closing trades.
- **`stop-loss` dominating exits with a much worse average loss than
  `stop_loss_pct` implies it should**: check for slippage/fill-price
  issues before assuming the stop level itself is wrong.
- **A `not_traded` reason firing constantly** (e.g. "no open slots" the
  large majority of cycles): this usually means a cap
  (`max_concurrent_positions`, `max_scout_positions`,
  `max_memecoin_exposure_fraction`) is the binding constraint, not the
  strategy or discovery thresholds -- worth knowing before concluding
  "nothing is eligible" or "the strategy never signals," which would be
  the wrong diagnosis.

## 3. Propose, don't apply

Write up what you found: the specific numbers, the sample size, and the
most likely explanation. Then propose ONE specific, minimal change --
naming the exact file and field (`config/risk.yaml`'s `max_scout_positions`,
`config/discovery.yaml`'s `safety.min_24h_volume_usd`, promoting/demoting a
strategy in `docs/STRATEGY.md`'s "Current strategies", etc.) -- and ask the
user before making it. Never bundle multiple speculative changes into one
"tune everything" pass; one evidence-backed change at a time keeps it
possible to tell afterward whether it actually helped.

If the pattern instead suggests a new strategy is worth building (e.g. the
data shows a specific exit reason or regime consistently mishandled), that
goes through the normal path: add it to `backtest/strategies.py`, backtest
it (`.claude/skills/backtest-strategy`), and paper-track it
(`python3 paper_trading/run_paper_cycle.py --strategy <new_strategy>`)
before it's a candidate for anything else -- this skill identifies the gap,
it doesn't skip the validation gate.

## 4. Log the review itself

Note in your response (and, if this becomes a recurring habit, worth its
own file) what you reviewed, what you found, what you proposed, and what
the user decided -- so a future review isn't re-deriving the same
conclusion from scratch, and can tell if a past change actually worked.

## Why this stays human-in-the-loop

Nothing in this skill edits `config/risk.yaml` or `config/discovery.yaml`
by itself, on the same principle as CLAUDE.md rule 1 (execution) and rule 7
(discovery thresholds): an automated system that quietly tunes its own risk
parameters based on a small, noisy sample of recent outcomes is a classic
way to overfit to noise and drift into something nobody actually reviewed
or approved. "Claude gets better at this over time" means the *evidence
base* compounds cycle over cycle -- not that the risk model silently
rewrites itself.
