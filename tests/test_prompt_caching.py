"""
Tests for the prompt-caching restructuring in module2_scoring,
module3_resume, and module4_coverletter (docs/PERFORMANCE_BASELINE.md
§ 6 / ARCHITECTURE.md "Prompt caching"): each module's user-message
content is now two text blocks - a stable, cache-marked prefix and a
volatile, unmarked suffix - instead of one flat string. These tests
prove the split doesn't change what Claude actually receives (for
module2/module4, byte-for-byte; for module3, which reorders content,
that no information was lost) and that the API call's other arguments
and each function's return value are unaffected.

No real API calls - client.messages.create is mocked throughout via
tracked_create.

Run: python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CANDIDATE_PROFILE
import modules.util as util
import modules.module2_scoring as m2
import modules.module3_resume as m3
import modules.module4_coverletter as m4

RESUME_TEXT = "JANE DOE\nSenior TPM with 8 years of experience.\n" + ("Bullet point. " * 50)


def _fake_response(text: str):
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    response.usage = MagicMock(
        input_tokens=100, output_tokens=50,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    return response


class _ResumeFileMixin:
    """Points MASTER_RESUME_PATH (already imported into the module under
    test) at a temp file with known content, so tests don't depend on
    the real data/master_resume.txt. Also redirects
    util.PIPELINE_STAGE_LOG_PATH to a tempfile, since score_job/
    tailor_resume/generate_cover_letter each run inside a real (unmocked)
    track_stage() block - without this, every test run here would write
    contamination into the real data/pipeline_stage_log.jsonl, exactly
    the kind of test-artifact pollution PERFORMANCE_BASELINE.md already
    had to back out of the llm_usage_log once.
    """

    module = None  # set by subclass

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        self.tmp.write(RESUME_TEXT)
        self.tmp.close()
        self._resume_patcher = patch.object(self.module, "MASTER_RESUME_PATH", self.tmp.name)
        self._resume_patcher.start()

        self._original_stage_log_path = util.PIPELINE_STAGE_LOG_PATH
        util.PIPELINE_STAGE_LOG_PATH = tempfile.mktemp(suffix=".jsonl")

    def tearDown(self):
        self._resume_patcher.stop()
        Path(self.tmp.name).unlink(missing_ok=True)
        util.PIPELINE_STAGE_LOG_PATH = self._original_stage_log_path


class TestModule2ScoringPromptSplit(_ResumeFileMixin, unittest.TestCase):
    module = m2

    def _score(self, job):
        with patch.object(m2, "tracked_create", return_value=_fake_response(
            json.dumps({"score": 80, "reasons": ["a", "b", "c"], "gaps": []})
        )) as mock_create:
            m2.score_job(job)
        return mock_create.call_args.kwargs

    def test_content_is_two_blocks_with_cache_control_on_the_first_only(self):
        kwargs = self._score({"title": "Sr TPM", "company": "Acme", "description": "Do the thing."})
        content = kwargs["messages"][0]["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", content[1])

    def test_stable_block_contains_profile_and_resume_not_job(self):
        kwargs = self._score({"title": "Sr TPM", "company": "Acme", "description": "Do the thing."})
        stable, volatile = kwargs["messages"][0]["content"]
        self.assertIn(CANDIDATE_PROFILE, stable["text"])
        self.assertIn(RESUME_TEXT[:3000], stable["text"])
        self.assertNotIn("Sr TPM", stable["text"])
        self.assertIn("Sr TPM", volatile["text"])
        self.assertIn("Acme", volatile["text"])
        self.assertIn("Do the thing.", volatile["text"])

    def test_concatenated_blocks_are_byte_identical_to_the_pre_split_prompt(self):
        job = {"title": "Sr TPM", "company": "Acme", "description": "Do the thing."}
        kwargs = self._score(job)
        stable, volatile = kwargs["messages"][0]["content"]

        expected = f"""Candidate profile:
{CANDIDATE_PROFILE}

Resume excerpt:
{RESUME_TEXT[:3000]}

Job posting:
Title: {job['title']}
Company: {job['company']}
Description:
{job['description'][:2000]}

Score this candidate's fit for this job."""
        self.assertEqual(stable["text"] + volatile["text"], expected)

    def test_system_prompt_and_other_kwargs_unaffected(self):
        kwargs = self._score({"title": "Sr TPM", "company": "Acme", "description": "Do the thing."})
        self.assertEqual(kwargs["system"], m2.SYSTEM_PROMPT)
        self.assertIsInstance(kwargs["system"], str)  # unchanged - not converted to a block list

    def test_return_value_unaffected(self):
        with patch.object(m2, "tracked_create", return_value=_fake_response(
            json.dumps({"score": 80, "reasons": ["a", "b", "c"], "gaps": []})
        )):
            result = m2.score_job({"title": "Sr TPM", "company": "Acme", "description": "Do the thing."})
        self.assertEqual(result["score"], 80)
        self.assertEqual(result["reasons"], ["a", "b", "c"])


class TestModule4CoverLetterPromptSplit(_ResumeFileMixin, unittest.TestCase):
    module = m4

    def _generate(self, job):
        with patch.object(m4, "tracked_create", return_value=_fake_response("Dear hiring team...")) as mock_create:
            m4.generate_cover_letter(job)
        return mock_create.call_args.kwargs

    def test_content_is_two_blocks_with_cache_control_on_the_first_only(self):
        kwargs = self._generate({"title": "Sr TPM", "company": "Acme", "description": "Do the thing."})
        content = kwargs["messages"][0]["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", content[1])

    def test_concatenated_blocks_are_byte_identical_to_the_pre_split_prompt(self):
        job = {"title": "Sr TPM", "company": "Acme", "description": "Do the thing."}
        kwargs = self._generate(job)
        stable, volatile = kwargs["messages"][0]["content"]

        expected = f"""Candidate profile:
{CANDIDATE_PROFILE}

Resume highlights:
{RESUME_TEXT[:2000]}

Job:
Title: {job['title']}
Company: {job['company']}
Description:
{job['description'][:2500]}

Write the cover letter."""
        self.assertEqual(stable["text"] + volatile["text"], expected)

    def test_return_value_unaffected(self):
        with patch.object(m4, "tracked_create", return_value=_fake_response("Dear hiring team...")):
            result = m4.generate_cover_letter({"title": "Sr TPM", "company": "Acme", "description": "d"})
        self.assertEqual(result, "Dear hiring team...")


class TestModule3ResumePromptSplit(_ResumeFileMixin, unittest.TestCase):
    module = m3

    def _tailor(self, job):
        with patch.object(m3, "tracked_create", return_value=_fake_response("TAILORED RESUME TEXT")) as mock_create:
            m3.tailor_resume(job)
        return mock_create.call_args.kwargs

    def test_content_is_two_blocks_with_cache_control_on_the_first_only(self):
        kwargs = self._tailor({"title": "Sr TPM", "company": "Acme", "description": "Do the thing."})
        content = kwargs["messages"][0]["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", content[1])

    def test_master_resume_moved_to_the_first_stable_block(self):
        # This module reorders content (job-then-resume -> resume-then-job)
        # so the stable content forms a prefix - required for caching, the
        # one deliberate prompt-content change in this round.
        kwargs = self._tailor({"title": "Sr TPM", "company": "Acme", "description": "Do the thing."})
        stable, volatile = kwargs["messages"][0]["content"]
        self.assertIn(RESUME_TEXT, stable["text"])
        self.assertNotIn("Sr TPM", stable["text"])
        self.assertIn("Sr TPM", volatile["text"])
        self.assertIn("Acme", volatile["text"])
        self.assertIn("Do the thing.", volatile["text"])
        self.assertNotIn(RESUME_TEXT, volatile["text"])

    def test_no_information_lost_by_the_reorder(self):
        job = {"title": "Sr TPM", "company": "Acme", "description": "Do the thing."}
        kwargs = self._tailor(job)
        stable, volatile = kwargs["messages"][0]["content"]
        combined = stable["text"] + volatile["text"]
        for expected_fragment in (
            RESUME_TEXT, job["title"], job["company"], job["description"],
            "Rewrite the resume to best match this job.",
        ):
            self.assertIn(expected_fragment, combined)

    def test_final_instruction_still_ends_the_prompt(self):
        kwargs = self._tailor({"title": "Sr TPM", "company": "Acme", "description": "Do the thing."})
        _, volatile = kwargs["messages"][0]["content"]
        self.assertTrue(volatile["text"].rstrip().endswith("Rewrite the resume to best match this job."))

    def test_return_value_unaffected(self):
        with patch.object(m3, "tracked_create", return_value=_fake_response("TAILORED RESUME TEXT")):
            result = m3.tailor_resume({"title": "Sr TPM", "company": "Acme", "description": "d"})
        self.assertEqual(result, "TAILORED RESUME TEXT")


if __name__ == "__main__":
    unittest.main()
