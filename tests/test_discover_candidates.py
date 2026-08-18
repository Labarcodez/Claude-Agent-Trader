"""Unit tests for research/discover_candidates.py's safety/tier logic --
mocked RugCheck responses, no real network calls (evaluate_candidate's stage-1
checks run on plain dicts; fetch_rugcheck_report is patched for stage 2).
Run: python3 -m unittest discover -s tests -v"""
import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from research import discover_candidates as disco  # noqa: E402


def make_args(**overrides):
    defaults = dict(
        min_liquidity_usd=250_000,
        min_holder_count=500,
        min_organic_score=40,
        min_pool_age_hours=72,
        max_top_holder_pct=20.0,
        require_mint_renounced=True,
        require_freeze_renounced=True,
        always_rugcheck=False,
        cross_check_dexscreener=False,
        request_delay=0,
        blue_chip_mcap_usd=50_000_000,
        blue_chip_holder_count=10_000,
        established_mcap_usd=5_000_000,
        established_holder_count=2_000,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def good_token(**overrides):
    tok = {
        "id": "MintAAA",
        "symbol": "GOOD",
        "liquidity": 1_000_000,
        "holderCount": 5_000,
        "mcap": 20_000_000,
        "fdv": 20_000_000,
        "organicScore": 80,
        "isVerified": True,
        "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 5.0},
        "firstPool": {"createdAt": "2020-01-01T00:00:00Z"},
    }
    tok.update(overrides)
    return tok


class TestEvaluateCandidateSafetyChecks(unittest.TestCase):
    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_good_token_is_eligible(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        result = disco.evaluate_candidate("MintAAA", good_token(), make_args())
        self.assertTrue(result["eligible"], result["reasons_fail"])

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_mint_authority_not_disabled_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        tok = good_token(audit={"mintAuthorityDisabled": False, "freezeAuthorityDisabled": True, "topHoldersPercentage": 5.0})
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("mint authority" in r for r in result["reasons_fail"]))

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_freeze_authority_not_disabled_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        tok = good_token(audit={"mintAuthorityDisabled": True, "freezeAuthorityDisabled": False, "topHoldersPercentage": 5.0})
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("freeze authority" in r for r in result["reasons_fail"]))

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_low_liquidity_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        result = disco.evaluate_candidate("MintAAA", good_token(liquidity=1000), make_args())
        self.assertFalse(result["eligible"])

    def test_low_liquidity_short_circuits_before_rugcheck_call(self):
        """RugCheck is a paid-in-request-volume resource -- a candidate that
        already fails stage 1 (liquidity) should never trigger a stage-2 call."""
        with patch("research.discover_candidates.fetch_rugcheck_report") as mock_rug:
            disco.evaluate_candidate("MintAAA", good_token(liquidity=1000), make_args())
            mock_rug.assert_not_called()

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_low_holder_count_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        result = disco.evaluate_candidate("MintAAA", good_token(holderCount=10), make_args())
        self.assertFalse(result["eligible"])

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_low_organic_score_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        result = disco.evaluate_candidate("MintAAA", good_token(organicScore=5), make_args())
        self.assertFalse(result["eligible"])

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_missing_organic_score_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        tok = good_token()
        tok["organicScore"] = None
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertFalse(result["eligible"])

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_high_top_holder_concentration_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        tok = good_token(audit={"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 60.0})
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("top holder" in r for r in result["reasons_fail"]))

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_young_pool_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        import datetime
        recent = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        tok = good_token(firstPool={"createdAt": recent})
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("pool age" in r for r in result["reasons_fail"]))

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_missing_pool_data_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        tok = good_token(firstPool=None)
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertFalse(result["eligible"])

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_confirmed_rug_rejects_even_if_jupiter_checks_pass(self, mock_rug):
        mock_rug.return_value = {"rugged": True, "risks": []}
        result = disco.evaluate_candidate("MintAAA", good_token(), make_args())
        self.assertFalse(result["eligible"])
        self.assertTrue(any("rug" in r.lower() for r in result["reasons_fail"]))

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_rugcheck_danger_risk_flag_rejects(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": [{"name": "Some scary thing", "level": "danger"}]}
        result = disco.evaluate_candidate("MintAAA", good_token(), make_args())
        self.assertFalse(result["eligible"])

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_rugcheck_unavailable_fails_safe(self, mock_rug):
        mock_rug.return_value = None
        result = disco.evaluate_candidate("MintAAA", good_token(), make_args())
        self.assertFalse(result["eligible"])

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_rugcheck_informational_risk_does_not_reject(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": [{"name": "Minor note", "level": "info"}]}
        result = disco.evaluate_candidate("MintAAA", good_token(), make_args())
        self.assertTrue(result["eligible"], result["reasons_fail"])


class TestTierClassification(unittest.TestCase):
    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_blue_chip_tier(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        tok = good_token(mcap=100_000_000, holderCount=20_000, isVerified=True)
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertEqual(result["tier"], "blue_chip")

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_not_blue_chip_when_unverified_despite_size(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        tok = good_token(mcap=100_000_000, holderCount=20_000, isVerified=False)
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertEqual(result["tier"], "established")

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_established_tier(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        tok = good_token(mcap=8_000_000, holderCount=3_000)
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertEqual(result["tier"], "established")

    @patch("research.discover_candidates.fetch_rugcheck_report")
    def test_emerging_tier_for_small_but_safe_token(self, mock_rug):
        mock_rug.return_value = {"rugged": False, "risks": []}
        tok = good_token(mcap=500_000, holderCount=600)
        result = disco.evaluate_candidate("MintAAA", tok, make_args())
        self.assertEqual(result["tier"], "emerging")

    def test_no_tier_assigned_to_rejected_candidate(self):
        with patch("research.discover_candidates.fetch_rugcheck_report"):
            result = disco.evaluate_candidate("MintAAA", good_token(liquidity=100), make_args())
            self.assertIsNone(result["tier"])


class TestPoolAgeHelper(unittest.TestCase):
    def test_none_when_missing(self):
        self.assertIsNone(disco._pool_age_hours(None))
        self.assertIsNone(disco._pool_age_hours({}))

    def test_computes_large_positive_age_for_old_date(self):
        age = disco._pool_age_hours({"createdAt": "2020-01-01T00:00:00Z"})
        self.assertGreater(age, 1000)

    def test_none_for_malformed_date(self):
        self.assertIsNone(disco._pool_age_hours({"createdAt": "not-a-date"}))


if __name__ == "__main__":
    unittest.main()
