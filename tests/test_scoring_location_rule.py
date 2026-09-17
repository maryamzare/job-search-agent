"""
Regression tests for the location-fit exception in module2_scoring.

The candidate profile states a Seattle/hybrid/remote preference. Without an
explicit carve-out, the scoring model treats any non-Seattle U.S. onsite role
as a location gap - including roles at large, well-known employers with
strong pay, where that penalty isn't wanted. This is implemented as a prompt
rule (not Python if-logic) because pay is usually absent from postings and
"is this a big company" is represented by a maintained company list.

No API calls - only checks the prompt text and the config constants it's
built from.

Run: python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import BIG_COMPANIES, HIGH_PAY_TC_USD, TARGET_LOCATIONS
import modules.module2_scoring as m2


class TestLocationRuleConfig(unittest.TestCase):
    def test_big_companies_is_a_nonempty_list_of_strings(self):
        self.assertIsInstance(BIG_COMPANIES, list)
        self.assertGreater(len(BIG_COMPANIES), 0)
        self.assertTrue(all(isinstance(c, str) and c for c in BIG_COMPANIES))

    def test_expected_companies_present(self):
        for company in ("Amazon", "Apple", "Google", "Microsoft", "Anthropic", "OpenAI"):
            self.assertIn(company, BIG_COMPANIES)

    def test_high_pay_threshold_is_user_selected_300k(self):
        self.assertEqual(HIGH_PAY_TC_USD, 300_000)

    def test_linkedin_search_includes_us_wide_jobs(self):
        self.assertIn("United States", TARGET_LOCATIONS)


class TestLocationRuleInPrompt(unittest.TestCase):
    def test_system_prompt_contains_the_location_rule(self):
        self.assertIn(m2.LOCATION_RULE, m2.SYSTEM_PROMPT)

    def test_rule_names_every_big_company(self):
        for company in BIG_COMPANIES:
            self.assertIn(company, m2.LOCATION_RULE)

    def test_rule_states_the_pay_threshold(self):
        self.assertIn(f"${HIGH_PAY_TC_USD:,}", m2.LOCATION_RULE)

    def test_rule_does_not_assume_unstated_pay_is_high(self):
        self.assertIn("missing pay is\nunknown", m2.LOCATION_RULE.lower())

    def test_rule_still_applies_normal_scoring_off_the_list(self):
        # The exception must not read as "any US location is fine" -
        # off-list companies / low pay should still score location normally.
        low = m2.LOCATION_RULE.lower()
        self.assertIn("if any condition is not established", low)
        self.assertIn("score location normally", low)

    def test_job_context_includes_location_and_compensation(self):
        from unittest.mock import MagicMock, patch
        response = MagicMock()
        response.content[0].text = '{"score": 80, "reasons": [], "gaps": []}'
        with patch.object(m2, "load_resume", return_value="resume"), \
             patch.object(m2, "tracked_create", return_value=response) as create, \
             patch.object(m2, "with_retry_sync", side_effect=lambda fn: fn()):
            m2.score_job({"title": "TPM", "company": "Apple", "location": "Austin, TX",
                          "salary": "$320,000 total compensation", "description": "Work"})
        context = create.call_args.kwargs["messages"][0]["content"][1]["text"]
        self.assertIn("Location: Austin, TX", context)
        self.assertIn("Compensation evidence from posting: $320,000 total compensation", context)

    def test_compensation_evidence_can_come_after_description_prefix(self):
        description = "A" * 2200 + "\nAnnual total compensation: $320,000."
        self.assertIn("total compensation: $320,000.",
                      m2._compensation_evidence({}, description))

    def test_json_schema_instructions_still_present(self):
        # The location rule is an addition, not a replacement of the existing
        # scoring contract.
        self.assertIn("score: integer 0-100", m2.SYSTEM_PROMPT)
        self.assertIn("Respond ONLY with valid JSON", m2.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
