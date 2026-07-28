"""
Integration tests proving module2_scoring.score_job, module3_resume.tailor_resume,
and module4_coverletter.generate_cover_letter actually retry through the real
with_retry_sync -> tracked_create -> client.messages.create call path - not
just that with_retry_sync works in isolation (tests/test_retry.py already
covers that exhaustively).

Only client.messages.create is faked (via side_effect); everything above it
(with_retry_sync, tracked_create, the module functions themselves) runs for
real. modules.util.time.sleep and both instrumentation log paths are patched
so these tests are instant and never touch real files - see the near-miss
documented in ARCHITECTURE.md "Prompt caching" this mirrors the fix for.

Run: python3 -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import anthropic
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modules.util as util
import modules.module2_scoring as m2
import modules.module3_resume as m3
import modules.module4_coverletter as m4

_FAKE_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

QUOTA_EXCEEDED_MESSAGE = (
    "You have reached your specified API usage limits. "
    "You will regain access on 2026-08-01 at 00:00 UTC."
)

RESUME_TEXT = "JANE DOE\nSenior TPM.\n" + ("Bullet point. " * 30)
JOB = {"title": "Sr TPM", "company": "Acme", "description": "Do the thing."}


def _status_error(cls, status_code, message="test error", headers=None):
    resp = httpx.Response(status_code, request=_FAKE_REQUEST, headers=headers or {})
    return cls(message, response=resp, body=None)


def _fake_success(text: str):
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    response.usage = MagicMock(
        input_tokens=10, output_tokens=5,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    return response


class _IsolatedEnvMixin:
    """Isolates both instrumentation logs and the master-resume file, and
    patches time.sleep so retry delays don't actually elapse.
    """

    module = None  # set by subclass

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        self.tmp.write(RESUME_TEXT)
        self.tmp.close()
        self._resume_patcher = patch.object(self.module, "MASTER_RESUME_PATH", self.tmp.name)
        self._resume_patcher.start()

        self._original_stage_log_path = util.PIPELINE_STAGE_LOG_PATH
        self._original_usage_log_path = util.LLM_USAGE_LOG_PATH
        util.PIPELINE_STAGE_LOG_PATH = tempfile.mktemp(suffix=".jsonl")
        util.LLM_USAGE_LOG_PATH = tempfile.mktemp(suffix=".jsonl")

        self._sleep_patcher = patch("modules.util.time.sleep")
        self._sleep_patcher.start()

    def tearDown(self):
        self._resume_patcher.stop()
        Path(self.tmp.name).unlink(missing_ok=True)
        util.PIPELINE_STAGE_LOG_PATH = self._original_stage_log_path
        util.LLM_USAGE_LOG_PATH = self._original_usage_log_path
        self._sleep_patcher.stop()


class TestModule2ScoringRetry(_IsolatedEnvMixin, unittest.TestCase):
    module = m2

    def test_successful_retry_recovery_after_transient_errors(self):
        create = MagicMock(side_effect=[
            _status_error(anthropic.InternalServerError, 503),
            _status_error(anthropic.InternalServerError, 503),
            _fake_success('{"score": 80, "reasons": ["a", "b", "c"], "gaps": []}'),
        ])
        with patch.object(m2.client.messages, "create", create):
            result = m2.score_job(dict(JOB))
        self.assertEqual(result["score"], 80)
        self.assertEqual(create.call_count, 3)

    def test_max_retry_exhaustion_raises(self):
        create = MagicMock(side_effect=_status_error(anthropic.InternalServerError, 503))
        with patch.object(m2.client.messages, "create", create):
            with self.assertRaises(anthropic.InternalServerError):
                m2.score_job(dict(JOB))
        self.assertEqual(create.call_count, 3)  # with_retry_sync's default retries=3

    def test_429_rate_limit_is_retried(self):
        create = MagicMock(side_effect=[
            _status_error(anthropic.RateLimitError, 429, "slow down"),
            _fake_success('{"score": 70, "reasons": ["a", "b", "c"], "gaps": []}'),
        ])
        with patch.object(m2.client.messages, "create", create):
            result = m2.score_job(dict(JOB))
        self.assertEqual(result["score"], 70)
        self.assertEqual(create.call_count, 2)

    def test_quota_exceeded_fails_fast_no_retry(self):
        create = MagicMock(side_effect=_status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE))
        with patch.object(m2.client.messages, "create", create):
            with self.assertRaises(anthropic.BadRequestError):
                m2.score_job(dict(JOB))
        self.assertEqual(create.call_count, 1, "quota exhaustion must not be retried")


class TestModule3ResumeRetry(_IsolatedEnvMixin, unittest.TestCase):
    module = m3

    def test_successful_retry_recovery_after_transient_errors(self):
        create = MagicMock(side_effect=[
            anthropic.APIConnectionError(request=_FAKE_REQUEST),
            _fake_success("TAILORED RESUME"),
        ])
        with patch.object(m3.client.messages, "create", create):
            result = m3.tailor_resume(dict(JOB))
        self.assertEqual(result, "TAILORED RESUME")
        self.assertEqual(create.call_count, 2)

    def test_max_retry_exhaustion_raises(self):
        create = MagicMock(side_effect=_status_error(anthropic.InternalServerError, 502))
        with patch.object(m3.client.messages, "create", create):
            with self.assertRaises(anthropic.InternalServerError):
                m3.tailor_resume(dict(JOB))
        self.assertEqual(create.call_count, 3)

    def test_429_rate_limit_is_retried(self):
        create = MagicMock(side_effect=[
            _status_error(anthropic.RateLimitError, 429, "slow down"),
            _fake_success("TAILORED RESUME"),
        ])
        with patch.object(m3.client.messages, "create", create):
            result = m3.tailor_resume(dict(JOB))
        self.assertEqual(result, "TAILORED RESUME")
        self.assertEqual(create.call_count, 2)

    def test_quota_exceeded_fails_fast_no_retry(self):
        create = MagicMock(side_effect=_status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE))
        with patch.object(m3.client.messages, "create", create):
            with self.assertRaises(anthropic.BadRequestError):
                m3.tailor_resume(dict(JOB))
        self.assertEqual(create.call_count, 1)


class TestModule4CoverLetterRetry(_IsolatedEnvMixin, unittest.TestCase):
    module = m4

    def test_successful_retry_recovery_after_transient_errors(self):
        create = MagicMock(side_effect=[
            _status_error(anthropic.InternalServerError, 500),
            _fake_success("COVER LETTER"),
        ])
        with patch.object(m4.client.messages, "create", create):
            result = m4.generate_cover_letter(dict(JOB))
        self.assertEqual(result, "COVER LETTER")
        self.assertEqual(create.call_count, 2)

    def test_max_retry_exhaustion_raises(self):
        create = MagicMock(side_effect=anthropic.APITimeoutError(request=_FAKE_REQUEST))
        with patch.object(m4.client.messages, "create", create):
            with self.assertRaises(anthropic.APITimeoutError):
                m4.generate_cover_letter(dict(JOB))
        self.assertEqual(create.call_count, 3)

    def test_429_rate_limit_is_retried(self):
        create = MagicMock(side_effect=[
            _status_error(anthropic.RateLimitError, 429, "slow down"),
            _fake_success("COVER LETTER"),
        ])
        with patch.object(m4.client.messages, "create", create):
            result = m4.generate_cover_letter(dict(JOB))
        self.assertEqual(result, "COVER LETTER")
        self.assertEqual(create.call_count, 2)

    def test_quota_exceeded_fails_fast_no_retry(self):
        create = MagicMock(side_effect=_status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE))
        with patch.object(m4.client.messages, "create", create):
            with self.assertRaises(anthropic.BadRequestError):
                m4.generate_cover_letter(dict(JOB))
        self.assertEqual(create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
