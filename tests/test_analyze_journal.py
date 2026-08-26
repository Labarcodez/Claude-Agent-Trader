"""Unit tests for scripts/analyze_journal.py -- synthetic journal entries,
no file I/O beyond a temp file this test creates and removes itself.
Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import analyze_journal as aj  # noqa: E402


def sell(pair="XBTUSD", tier="blue_chip", strategy="adaptive_ensemble", reason="stop-loss (-15.0%)", return_pct=-15.0):
    return {"type": "sell", "pair": pair, "tier": tier, "strategy": strategy, "reason": reason, "return_pct": return_pct}


def partial_sell(pair="DOGEUSD", tier="scout", strategy="adaptive_ensemble", return_pct=None):
    return {"type": "partial_sell", "pair": pair, "tier": tier, "strategy": strategy,
            "reason": "profit-take (2.0x cost basis)"}


class TestLoadCycles(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parent / "_tmp_journal_test.jsonl"

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(aj.load_cycles(self.path), [])

    def test_round_trip_through_jsonl_lines(self):
        self.path.write_text('{"a": 1}\n{"b": 2}\n')
        self.assertEqual(aj.load_cycles(self.path), [{"a": 1}, {"b": 2}])

    def test_malformed_line_is_skipped_not_a_crash(self):
        self.path.write_text('{"a": 1}\nnot valid json\n{"b": 2}\n')
        self.assertEqual(aj.load_cycles(self.path), [{"a": 1}, {"b": 2}])

    def test_blank_lines_are_ignored(self):
        self.path.write_text('{"a": 1}\n\n\n{"b": 2}\n')
        self.assertEqual(aj.load_cycles(self.path), [{"a": 1}, {"b": 2}])


class TestExtractClosedTrades(unittest.TestCase):
    def test_pulls_sell_and_partial_sell_actions_from_every_cycle(self):
        cycles = [
            {"actions": [sell(), {"type": "buy", "pair": "ETHUSD"}]},
            {"actions": [partial_sell()]},
        ]
        trades = aj.extract_closed_trades(cycles)
        self.assertEqual(len(trades), 2)
        self.assertEqual({t["type"] for t in trades}, {"sell", "partial_sell"})

    def test_cycle_with_no_actions_key_does_not_crash(self):
        self.assertEqual(aj.extract_closed_trades([{"timestamp": "x"}]), [])

    def test_empty_cycles_list_returns_empty(self):
        self.assertEqual(aj.extract_closed_trades([]), [])


class TestNotTradedReasonCounts(unittest.TestCase):
    def test_collapses_reasons_that_differ_only_by_an_embedded_dollar_amount(self):
        # the dollar figure appears BEFORE the parenthetical here, so
        # paren-stripping alone wouldn't collapse these -- the numeric
        # normalization is what makes them the same bucket
        cycles = [
            {"not_traded": {"AUSD": {"reason": "sized position $4.10 below min_trade_usd ($5.00)"}}},
            {"not_traded": {"BUSD": {"reason": "sized position $3.00 below min_trade_usd ($5.00)"}}},
        ]
        counts = aj.not_traded_reason_counts(cycles)
        self.assertEqual(len(counts), 1, "both reasons should collapse into one bucket")
        self.assertEqual(sum(counts.values()), 2)

    def test_collapses_reasons_that_differ_only_by_a_parenthetical_count(self):
        cycles = [
            {"not_traded": {"AUSD": {"reason": "no open slots (max_concurrent_positions=15)"}}},
            {"not_traded": {"BUSD": {"reason": "no open slots (max_concurrent_positions=15)"}}},
        ]
        counts = aj.not_traded_reason_counts(cycles)
        self.assertEqual(len(counts), 1)
        self.assertEqual(sum(counts.values()), 2)

    def test_distinct_reasons_stay_in_separate_buckets(self):
        cycles = [
            {"not_traded": {"AUSD": {"reason": "no open slots (max_concurrent_positions=15)"}}},
            {"not_traded": {"BUSD": {"reason": "strategy signal was 'hold', not buy"}}},
        ]
        counts = aj.not_traded_reason_counts(cycles)
        self.assertEqual(len(counts), 2)

    def test_no_not_traded_key_does_not_crash(self):
        self.assertEqual(aj.not_traded_reason_counts([{"timestamp": "x"}]), {})

    def test_string_valued_not_traded_entries_are_handled(self):
        cycles = [{"not_traded": {"AUSD": "regime filter blocking new entries (risk-OFF)"}}]
        counts = aj.not_traded_reason_counts(cycles)
        self.assertEqual(sum(counts.values()), 1)


class TestSummarize(unittest.TestCase):
    def test_groups_by_tier(self):
        trades = [sell(tier="blue_chip", return_pct=10.0), sell(tier="blue_chip", return_pct=-5.0),
                  sell(tier="emerging", return_pct=20.0)]
        result = aj.summarize(trades, "tier")
        self.assertEqual(result["blue_chip"]["n"], 2)
        self.assertAlmostEqual(result["blue_chip"]["avg_return_pct"], 2.5)
        self.assertAlmostEqual(result["blue_chip"]["win_rate_pct"], 50.0)
        self.assertEqual(result["emerging"]["n"], 1)
        self.assertAlmostEqual(result["emerging"]["win_rate_pct"], 100.0)

    def test_excludes_partial_sells(self):
        trades = [sell(return_pct=10.0), partial_sell()]
        result = aj.summarize(trades, "tier")
        total_n = sum(s["n"] for s in result.values())
        self.assertEqual(total_n, 1)

    def test_reason_grouped_by_stable_prefix(self):
        trades = [sell(reason="stop-loss (-15.0%)", return_pct=-15.0),
                  sell(reason="stop-loss (-16.2%)", return_pct=-16.2)]
        result = aj.summarize(trades, "reason")
        self.assertEqual(list(result.keys()), ["stop-loss"])
        self.assertEqual(result["stop-loss"]["n"], 2)

    def test_missing_group_key_falls_back_to_unknown(self):
        trades = [{"type": "sell", "return_pct": 5.0}]  # no "tier" key at all
        result = aj.summarize(trades, "tier")
        self.assertIn("unknown", result)

    def test_empty_trades_returns_empty_dict(self):
        self.assertEqual(aj.summarize([], "tier"), {})

    def test_trade_with_no_return_pct_is_skipped(self):
        trades = [{"type": "sell", "tier": "blue_chip", "return_pct": None}]
        self.assertEqual(aj.summarize(trades, "tier"), {})


if __name__ == "__main__":
    unittest.main()
