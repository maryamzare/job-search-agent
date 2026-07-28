"""
Tests for modules.module2b_board_review's prompt-context fix
(PERFORMANCE_BASELINE.md § 3 / ARCHITECTURE.md "Module 2b prompt
context"): reviewers now receive only a job's posting fields, not its
full accumulated pipeline history, and that content is byte-identical
across reviewers and stable across repeated runs of the same job.

No real API calls - tracked_create_async is mocked throughout.

Run: python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modules.util as util
from modules.module2b_board_review import POSTING_FIELDS, _posting_context, run_advisory_board


def _fake_response(payload: dict):
    response = AsyncMock()
    response.content = [type("Block", (), {"text": json.dumps(payload)})()]
    return response


class TestPostingContext(unittest.TestCase):
    def test_extracts_only_posting_fields(self):
        job = {
            "title": "Sr TPM", "company": "Acme", "location": "Seattle",
            "url": "https://x/1", "description": "Do the thing.",
            "fit_score": 91, "fit_reasons": ["a"], "fit_gaps": ["b"],
            "status": "shortlisted", "shortlisted_at": "2026-07-01T00:00:00+00:00",
            "board_reviews": {"fit_reviewer": {"score": 8}},
            "board_decision": {"action": "apply"},
            "resume_board": {"x": 1}, "resume_scorecard": {"composite_score": 7},
            "resume_v2_path": "outputs/x.txt", "notes": ["a note"],
        }
        posting = _posting_context(job)
        self.assertEqual(set(posting.keys()), set(POSTING_FIELDS))
        self.assertEqual(posting["title"], "Sr TPM")
        self.assertEqual(posting["company"], "Acme")
        self.assertEqual(posting["description"], "Do the thing.")
        self.assertNotIn("fit_score", posting)
        self.assertNotIn("board_reviews", posting)
        self.assertNotIn("resume_scorecard", posting)
        self.assertNotIn("status", posting)

    def test_missing_fields_default_to_empty_string_not_a_crash(self):
        posting = _posting_context({"title": "TPM"})
        self.assertEqual(posting["company"], "")
        self.assertEqual(posting["description"], "")

    def test_shape_is_identical_regardless_of_accumulated_history(self):
        fresh = {"title": "TPM", "company": "Acme", "description": "d"}
        processed = dict(
            fresh, fit_score=91, board_reviews={"x": 1},
            resume_scorecard={"composite_score": 7}, status="board_approved",
        )
        self.assertEqual(_posting_context(fresh), _posting_context(processed))


class TestRunAdvisoryBoardPromptContent(unittest.IsolatedAsyncioTestCase):
    REVIEWER_PAYLOAD = {"score": 8, "verdict": "strong", "key_matches": [], "gaps": []}
    CHAIR_PAYLOAD = {
        "composite_score": 8, "action": "apply", "summary": "ok",
        "top_concern": "", "top_strength": "",
    }

    def setUp(self):
        # run_advisory_board runs inside a real (unmocked) track_stage()
        # block - redirect its log target so these tests don't write into
        # the real data/pipeline_stage_log.jsonl.
        self._original_stage_log_path = util.PIPELINE_STAGE_LOG_PATH
        util.PIPELINE_STAGE_LOG_PATH = tempfile.mktemp(suffix=".jsonl")

    def tearDown(self):
        util.PIPELINE_STAGE_LOG_PATH = self._original_stage_log_path

    async def _run(self, job):
        calls = []

        async def fake_tracked_create_async(client, label, **kwargs):
            calls.append((label, kwargs))
            payload = self.CHAIR_PAYLOAD if label == "board_review:chair" else self.REVIEWER_PAYLOAD
            return _fake_response(payload)

        with patch(
            "modules.module2b_board_review.tracked_create_async",
            new=fake_tracked_create_async,
        ):
            result = await run_advisory_board(job, "RESUME TEXT")
        return result, calls

    @staticmethod
    def _reviewer_contents(calls):
        return [
            kwargs["messages"][0]["content"]
            for label, kwargs in calls
            if label != "board_review:chair"
        ]

    async def test_reviewer_content_excludes_pipeline_history_fields(self):
        job = {
            "title": "Sr TPM", "company": "Acme", "description": "Do the thing.",
            "fit_score": 91, "fit_reasons": ["a"], "board_reviews": {"old": True},
            "resume_scorecard": {"composite_score": 7}, "status": "shortlisted",
        }
        _, calls = await self._run(job)
        contents = self._reviewer_contents(calls)
        self.assertEqual(len(contents), 4)
        for content in contents:
            self.assertNotIn("fit_score", content)
            self.assertNotIn("fit_reasons", content)
            self.assertNotIn("board_reviews", content)
            self.assertNotIn("resume_scorecard", content)

    async def test_reviewer_content_still_includes_posting_fields_and_resume(self):
        job = {"title": "Sr TPM", "company": "Acme", "description": "Do the thing."}
        _, calls = await self._run(job)
        for content in self._reviewer_contents(calls):
            self.assertIn("Sr TPM", content)
            self.assertIn("Acme", content)
            self.assertIn("Do the thing.", content)
            self.assertIn("RESUME TEXT", content)

    async def test_content_is_byte_identical_across_all_four_reviewers(self):
        job = {"title": "Sr TPM", "company": "Acme", "description": "Do the thing."}
        _, calls = await self._run(job)
        contents = set(self._reviewer_contents(calls))
        self.assertEqual(len(contents), 1)

    async def test_content_is_stable_across_repeated_runs_despite_accumulated_history(self):
        fresh_job = {"title": "Sr TPM", "company": "Acme", "description": "Do the thing."}
        _, first_calls = await self._run(dict(fresh_job))

        processed_job = dict(
            fresh_job, fit_score=91, status="board_approved",
            board_reviews={"fit_reviewer": {"score": 8}},
            resume_scorecard={"composite_score": 7},
        )
        _, second_calls = await self._run(processed_job)

        self.assertEqual(
            self._reviewer_contents(first_calls)[0],
            self._reviewer_contents(second_calls)[0],
        )

    async def test_output_shape_and_values_unaffected_by_the_change(self):
        job = {"title": "Sr TPM", "company": "Acme", "description": "Do the thing."}
        result, _ = await self._run(job)
        self.assertEqual(set(result.keys()), {"reviews", "board_decision"})
        self.assertEqual(
            set(result["reviews"].keys()),
            {"fit_reviewer", "strategy_reviewer", "risk_reviewer", "effort_reviewer"},
        )
        self.assertEqual(result["board_decision"]["action"], "apply")
        self.assertEqual(result["board_decision"]["composite_score"], 8)


if __name__ == "__main__":
    unittest.main()
