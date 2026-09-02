"""
Tests that module5_apply.apply_to_shortlisted() only ever pulls from the
statuses it's meant to: "shortlisted", "board_approved", and "in_progress".
In particular, "applied" and "rejected" jobs must never resurface here -
regressing this was bug #2 from the pre-v2 code review (a job answered
"n" in a prior run became unreachable; the inverse failure would be an
already-applied or already-rejected job reappearing and getting a
duplicate application attempt).

No real browser/network calls - print_application_materials and
webbrowser.open are patched throughout.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modules.module5_apply as apply_module


def _job(company, status, **extra):
    return {"company": company, "title": "TPM", "status": status, "fit_score": 80, **extra}


class TestApplyQueueExclusions(unittest.TestCase):
    def _companies_offered(self, jobs):
        """Return the companies apply_to_shortlisted actually surfaces to
        print_application_materials, by patching it to record calls and
        report the resume as not ready (so no input() prompt blocks)."""
        queue = {"jobs": jobs}
        seen = []

        def _fake_materials(job):
            seen.append(job["company"])
            return False  # not ready -> skipped without prompting for input

        with patch.object(apply_module, "load_queue", return_value=queue), \
             patch.object(apply_module, "print_application_materials", side_effect=_fake_materials):
            apply_module.apply_to_shortlisted()
        return seen

    def test_applied_job_never_reoffered(self):
        jobs = [_job("Already-Applied", "applied"), _job("Still-Open", "shortlisted")]
        seen = self._companies_offered(jobs)
        self.assertNotIn("Already-Applied", seen)
        self.assertIn("Still-Open", seen)

    def test_rejected_job_never_reoffered(self):
        jobs = [_job("Rejected-Co", "rejected"), _job("Still-Open", "board_approved")]
        seen = self._companies_offered(jobs)
        self.assertNotIn("Rejected-Co", seen)
        self.assertIn("Still-Open", seen)

    def test_closed_interviewing_and_offer_jobs_never_reoffered(self):
        jobs = [
            _job("Closed-Co", "closed"),
            _job("Interviewing-Co", "interviewing"),
            _job("Offer-Co", "offer"),
            _job("Still-Open", "shortlisted"),
        ]
        seen = self._companies_offered(jobs)
        self.assertNotIn("Closed-Co", seen)
        self.assertNotIn("Interviewing-Co", seen)
        self.assertNotIn("Offer-Co", seen)
        self.assertIn("Still-Open", seen)

    def test_in_progress_shortlisted_and_board_approved_are_offered(self):
        jobs = [
            _job("InProgress-Co", "in_progress"),
            _job("Shortlisted-Co", "shortlisted"),
            _job("BoardApproved-Co", "board_approved"),
        ]
        seen = self._companies_offered(jobs)
        self.assertEqual(
            set(seen), {"InProgress-Co", "Shortlisted-Co", "BoardApproved-Co"}
        )

    def test_discovered_and_filtered_out_are_not_offered(self):
        jobs = [_job("Discovered-Co", "discovered"), _job("Filtered-Co", "filtered_out")]
        seen = self._companies_offered(jobs)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
