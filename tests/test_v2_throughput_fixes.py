"""Regression tests for the v2 interview-throughput fixes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import modules.module1_discovery as discovery
import modules.module2_scoring as scoring
import modules.module5_apply as apply_module
from modules.util import job_artifact_key


class TestPostingIdentity(unittest.TestCase):
    def test_same_title_new_url_gets_a_different_artifact(self):
        first = {"company": "Acme", "title": "TPM", "url": "https://x/1", "description": "A"}
        repost = {"company": "Acme", "title": "TPM", "url": "https://x/2", "description": "B"}
        self.assertNotEqual(job_artifact_key(first), job_artifact_key(repost))

    def test_discovery_accepts_repost_with_same_company_and_title(self):
        queue = {"jobs": [{"company": "Acme", "title": "TPM", "url": "https://x/1"}]}
        repost = {"company": "Acme", "title": "TPM", "url": "https://x/2"}
        with patch.object(discovery, "load_queue", return_value=queue), patch.object(discovery, "save_queue"):
            self.assertEqual(discovery.add_new_jobs_to_queue([repost]), 1)
        self.assertEqual(queue["jobs"][-1]["url"], "https://x/2")


class TestScoringFailure(unittest.TestCase):
    def test_invalid_json_raises_instead_of_becoming_score_zero(self):
        response = MagicMock()
        response.content = [MagicMock(text="not json")]
        with patch.object(scoring, "tracked_create", return_value=response), \
             patch.object(scoring, "load_resume", return_value="resume"), \
             patch.object(scoring, "track_stage", return_value=unittest.mock.MagicMock(
                 __enter__=lambda self: None, __exit__=lambda self, *args: False
             )):
            with self.assertRaises(ValueError):
                scoring.score_job({"title": "TPM", "company": "Acme", "description": "job"})


class TestApplyQueue(unittest.TestCase):
    def test_in_progress_is_included_and_processed_first(self):
        jobs = [
            {"status": "shortlisted", "fit_score": 99, "company": "High", "title": "TPM"},
            {"status": "in_progress", "fit_score": 70, "company": "Finish", "title": "TPM"},
        ]
        queue = {"jobs": jobs}
        with patch.object(apply_module, "load_queue", return_value=queue), \
             patch.object(apply_module, "print_application_materials", return_value=False) as materials:
            apply_module.apply_to_shortlisted()
        self.assertEqual(materials.call_args_list[0].args[0]["company"], "Finish")


if __name__ == "__main__":
    unittest.main()
