"""
Tests for modules.util.with_retry / is_transient_error.

Covers the reliability bug found during evaluation: module3b_resume_board's
retry logic used to retry ANY APIStatusError, including a quota-exceeded
400 that can never succeed on retry. These tests pin down the fixed
behavior: only network errors, timeouts, and 500/502/503/504 responses are
retried; everything else (quota exceeded, auth failures, invalid requests,
429 rate limits) fails on the first attempt.

Run: python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anthropic
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.util import is_transient_error, with_retry

_FAKE_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status_code):
    resp = httpx.Response(status_code, request=_FAKE_REQUEST)
    return cls("test error", response=resp, body=None)


class TestIsTransientError(unittest.TestCase):
    def test_connection_error_is_transient(self):
        exc = anthropic.APIConnectionError(request=_FAKE_REQUEST)
        self.assertTrue(is_transient_error(exc))

    def test_timeout_error_is_transient(self):
        # APITimeoutError is a subclass of APIConnectionError in the SDK.
        exc = anthropic.APITimeoutError(request=_FAKE_REQUEST)
        self.assertTrue(is_transient_error(exc))

    def test_500_internal_server_error_is_transient(self):
        self.assertTrue(is_transient_error(_status_error(anthropic.InternalServerError, 500)))

    def test_502_503_504_are_transient(self):
        for code in (502, 503, 504):
            with self.subTest(code=code):
                self.assertTrue(is_transient_error(_status_error(anthropic.InternalServerError, code)))

    def test_400_quota_exceeded_is_not_transient(self):
        # This is the exact real-world failure that motivated this fix:
        # Anthropic returns quota-exceeded as a 400 invalid_request_error.
        self.assertFalse(is_transient_error(_status_error(anthropic.BadRequestError, 400)))

    def test_401_authentication_failure_is_not_transient(self):
        self.assertFalse(is_transient_error(_status_error(anthropic.AuthenticationError, 401)))

    def test_403_permission_denied_is_not_transient(self):
        self.assertFalse(is_transient_error(_status_error(anthropic.PermissionDeniedError, 403)))

    def test_404_not_found_is_not_transient(self):
        self.assertFalse(is_transient_error(_status_error(anthropic.NotFoundError, 404)))

    def test_429_rate_limit_is_not_transient_by_design(self):
        # Deliberately excluded from the retryable set for now - see the
        # TRANSIENT_HTTP_STATUS_CODES comment in modules/util.py and
        # ARCHITECTURE.md for the reasoning and how to revisit this.
        self.assertFalse(is_transient_error(_status_error(anthropic.RateLimitError, 429)))

    def test_non_anthropic_exception_is_not_transient(self):
        self.assertFalse(is_transient_error(ValueError("not an anthropic error at all")))


class TestWithRetry(unittest.IsolatedAsyncioTestCase):
    """Proves: (1) transient errors retry and can succeed, (2) non-transient
    errors fail immediately with zero retries, (3) retry count/backoff are
    correct for both the exhausted-retries and successful-after-retry cases.
    """

    async def test_transient_error_retries_then_succeeds(self):
        attempts = []

        async def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise _status_error(anthropic.InternalServerError, 503)
            return "success"

        with patch("modules.util.asyncio.sleep", new=AsyncMock()):
            result = await with_retry(flaky, retries=3, base_delay=0.01)

        self.assertEqual(result, "success")
        self.assertEqual(len(attempts), 3, "should have taken exactly 3 attempts to succeed")

    async def test_quota_exceeded_fails_immediately_no_retry(self):
        attempts = []

        async def always_quota_exceeded():
            attempts.append(1)
            raise _status_error(anthropic.BadRequestError, 400)

        with self.assertRaises(anthropic.BadRequestError):
            await with_retry(always_quota_exceeded, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1, "a quota-exceeded (400) error must not be retried at all")

    async def test_authentication_failure_fails_immediately_no_retry(self):
        attempts = []

        async def always_auth_error():
            attempts.append(1)
            raise _status_error(anthropic.AuthenticationError, 401)

        with self.assertRaises(anthropic.AuthenticationError):
            await with_retry(always_auth_error, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1, "an authentication failure must not be retried at all")

    async def test_transient_error_exhausts_retries_and_raises(self):
        attempts = []

        async def always_503():
            attempts.append(1)
            raise _status_error(anthropic.InternalServerError, 503)

        with patch("modules.util.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(anthropic.InternalServerError):
                await with_retry(always_503, retries=3, base_delay=0.01)

        self.assertEqual(len(attempts), 3, "should attempt exactly `retries` times before giving up")

    async def test_backoff_delay_doubles_each_retry(self):
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        async def always_503():
            raise _status_error(anthropic.InternalServerError, 503)

        with patch("modules.util.asyncio.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.InternalServerError):
                await with_retry(always_503, retries=3, base_delay=2.0)

        # 3 attempts -> 2 sleeps in between, doubling from base_delay each time
        self.assertEqual(delays, [2.0, 4.0])

    async def test_no_sleep_at_all_when_error_is_non_transient(self):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        async def always_quota_exceeded():
            raise _status_error(anthropic.BadRequestError, 400)

        with patch("modules.util.asyncio.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.BadRequestError):
                await with_retry(always_quota_exceeded, retries=5, base_delay=2.0)

        self.assertEqual(slept, [], "non-transient errors must never trigger a backoff sleep")


if __name__ == "__main__":
    unittest.main()
