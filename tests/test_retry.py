"""
Tests for modules.util's retry policy: classify_error, is_transient_error,
is_quota_exceeded_error, with_retry, and its synchronous counterpart
with_retry_sync (used by module2_scoring, module3_resume, and
module4_coverletter, which are built on the sync Anthropic client).

Covers three separately-handled cases:

1. HTTP 429 rate limiting - retried with exponential backoff, honoring a
   Retry-After header when the response carries one.
2. Account quota exhaustion (Anthropic reports this as free text inside a
   400, e.g. "...You will regain access on <date>.") - never retried,
   fails immediately, so callers can catch it and persist recovery state
   (see modules/eval_recovery.py).
3. Other non-retryable errors (auth failure, permission error, invalid
   request, not found) - never retried, no special notification beyond
   the exception itself.

Also covers the original reliability bug this policy replaced:
module3b_resume_board's old retry logic caught ANY anthropic.APIStatusError,
so a quota-exceeded 400 was retried 5 times with exponential backoff before
failing anyway.

Run: python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anthropic
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.util import (
    ERROR_NON_RETRYABLE,
    ERROR_QUOTA_EXCEEDED,
    ERROR_RATE_LIMITED,
    ERROR_TRANSIENT,
    classify_error,
    is_quota_exceeded_error,
    is_transient_error,
    with_retry,
    with_retry_sync,
)

_FAKE_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

QUOTA_EXCEEDED_MESSAGE = (
    "You have reached your specified API usage limits. "
    "You will regain access on 2026-08-01 at 00:00 UTC."
)


def _status_error(cls, status_code, message="test error", headers=None):
    resp = httpx.Response(status_code, request=_FAKE_REQUEST, headers=headers or {})
    return cls(message, response=resp, body=None)


class TestClassifyError(unittest.TestCase):
    """Proves the 4-way classification, including the priority rule that a
    quota-exceeded message wins even if it arrived on an unexpected status
    code (checked before the 429/5xx checks - see classify_error's
    docstring for why)."""

    def test_429_without_quota_language_is_rate_limited(self):
        exc = _status_error(anthropic.RateLimitError, 429, message="Rate limit exceeded, please slow down")
        self.assertEqual(classify_error(exc), ERROR_RATE_LIMITED)

    def test_400_with_quota_language_is_quota_exceeded(self):
        exc = _status_error(anthropic.BadRequestError, 400, message=QUOTA_EXCEEDED_MESSAGE)
        self.assertEqual(classify_error(exc), ERROR_QUOTA_EXCEEDED)

    def test_429_with_quota_language_is_still_quota_exceeded(self):
        # Priority check: quota-exhaustion wording takes precedence over
        # the status code, in case Anthropic (or any provider) ever
        # reports a hard quota cap via 429 instead of 400.
        exc = _status_error(anthropic.RateLimitError, 429, message=QUOTA_EXCEEDED_MESSAGE)
        self.assertEqual(classify_error(exc), ERROR_QUOTA_EXCEEDED)

    def test_generic_400_without_quota_language_is_non_retryable(self):
        exc = _status_error(anthropic.BadRequestError, 400, message="messages: roles must alternate")
        self.assertEqual(classify_error(exc), ERROR_NON_RETRYABLE)

    def test_connection_error_is_transient(self):
        exc = anthropic.APIConnectionError(request=_FAKE_REQUEST)
        self.assertEqual(classify_error(exc), ERROR_TRANSIENT)

    def test_timeout_error_is_transient(self):
        # APITimeoutError is a subclass of APIConnectionError in the SDK.
        exc = anthropic.APITimeoutError(request=_FAKE_REQUEST)
        self.assertEqual(classify_error(exc), ERROR_TRANSIENT)

    def test_500_502_503_504_are_transient(self):
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                exc = _status_error(anthropic.InternalServerError, code)
                self.assertEqual(classify_error(exc), ERROR_TRANSIENT)

    def test_401_authentication_failure_is_non_retryable(self):
        exc = _status_error(anthropic.AuthenticationError, 401)
        self.assertEqual(classify_error(exc), ERROR_NON_RETRYABLE)

    def test_403_permission_denied_is_non_retryable(self):
        exc = _status_error(anthropic.PermissionDeniedError, 403)
        self.assertEqual(classify_error(exc), ERROR_NON_RETRYABLE)

    def test_404_not_found_is_non_retryable(self):
        exc = _status_error(anthropic.NotFoundError, 404)
        self.assertEqual(classify_error(exc), ERROR_NON_RETRYABLE)

    def test_non_anthropic_exception_is_non_retryable(self):
        self.assertEqual(classify_error(ValueError("not an anthropic error at all")), ERROR_NON_RETRYABLE)


class TestIsTransientErrorAndIsQuotaExceededError(unittest.TestCase):
    """These two predicates are thin views over classify_error - kept as
    separate public functions since callers rarely need the full 4-way
    split (with_retry does; most other code just wants "should I retry
    this?" or "is the account out of quota?")."""

    def test_is_transient_error_true_for_rate_limited_and_transient(self):
        self.assertTrue(is_transient_error(_status_error(anthropic.RateLimitError, 429, "slow down")))
        self.assertTrue(is_transient_error(_status_error(anthropic.InternalServerError, 503)))
        self.assertTrue(is_transient_error(anthropic.APIConnectionError(request=_FAKE_REQUEST)))

    def test_is_transient_error_false_for_quota_exceeded_and_non_retryable(self):
        self.assertFalse(is_transient_error(_status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE)))
        self.assertFalse(is_transient_error(_status_error(anthropic.AuthenticationError, 401)))

    def test_is_quota_exceeded_error_true_only_for_quota_message(self):
        self.assertTrue(is_quota_exceeded_error(_status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE)))

    def test_is_quota_exceeded_error_false_for_rate_limit_and_others(self):
        self.assertFalse(is_quota_exceeded_error(_status_error(anthropic.RateLimitError, 429, "slow down")))
        self.assertFalse(is_quota_exceeded_error(_status_error(anthropic.InternalServerError, 503)))
        self.assertFalse(is_quota_exceeded_error(_status_error(anthropic.AuthenticationError, 401)))


class TestWithRetryTransientAnd5xx(unittest.IsolatedAsyncioTestCase):
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


class TestWithRetryRateLimited(unittest.IsolatedAsyncioTestCase):
    """Case 1 from the design: HTTP 429 retries with exponential backoff,
    honoring Retry-After when present."""

    async def test_rate_limited_retries_then_succeeds(self):
        attempts = []

        async def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise _status_error(anthropic.RateLimitError, 429, "slow down")
            return "success"

        with patch("modules.util.asyncio.sleep", new=AsyncMock()):
            result = await with_retry(flaky, retries=3, base_delay=0.01)

        self.assertEqual(result, "success")
        self.assertEqual(len(attempts), 3)

    async def test_rate_limited_without_retry_after_header_uses_exponential_backoff(self):
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        async def always_429():
            raise _status_error(anthropic.RateLimitError, 429, "slow down")  # no retry-after header

        with patch("modules.util.asyncio.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.RateLimitError):
                await with_retry(always_429, retries=3, base_delay=2.0)

        self.assertEqual(delays, [2.0, 4.0])

    async def test_rate_limited_with_retry_after_header_honors_it_instead_of_backoff(self):
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        async def always_429():
            raise _status_error(anthropic.RateLimitError, 429, "slow down", headers={"retry-after": "15"})

        with patch("modules.util.asyncio.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.RateLimitError):
                await with_retry(always_429, retries=3, base_delay=2.0)

        # Retry-After: 15 used on every retry attempt instead of 2.0/4.0 exponential backoff
        self.assertEqual(delays, [15.0, 15.0])

    async def test_retry_after_value_is_capped_at_max_delay(self):
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        async def always_429():
            raise _status_error(anthropic.RateLimitError, 429, "slow down", headers={"retry-after": "99999"})

        with patch("modules.util.asyncio.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.RateLimitError):
                await with_retry(always_429, retries=2, base_delay=2.0, max_delay=30.0)

        self.assertEqual(delays, [30.0], "an absurd Retry-After value must be capped, not slept on directly")

    async def test_non_numeric_retry_after_falls_back_to_exponential_backoff(self):
        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        async def always_429():
            raise _status_error(anthropic.RateLimitError, 429, "slow down", headers={"retry-after": "not-a-number"})

        with patch("modules.util.asyncio.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.RateLimitError):
                await with_retry(always_429, retries=2, base_delay=2.0)

        self.assertEqual(delays, [2.0])


class TestWithRetryQuotaExceeded(unittest.IsolatedAsyncioTestCase):
    """Case 2 from the design: account quota exhaustion fails fast, is
    never retried, and (per modules/eval_recovery.py's design) the
    exception is re-raised unmodified so a caller can persist recovery
    state and notify the user - proven end-to-end in
    tests/test_eval_recovery.py's run_recovery_check tests."""

    async def test_quota_exceeded_fails_immediately_no_retry(self):
        attempts = []

        async def always_quota_exceeded():
            attempts.append(1)
            raise _status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE)

        with self.assertRaises(anthropic.BadRequestError):
            await with_retry(always_quota_exceeded, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1, "a quota-exceeded error must not be retried at all")

    async def test_quota_exceeded_never_sleeps(self):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        async def always_quota_exceeded():
            raise _status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE)

        with patch("modules.util.asyncio.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.BadRequestError):
                await with_retry(always_quota_exceeded, retries=5, base_delay=2.0)

        self.assertEqual(slept, [], "quota exhaustion must never trigger a backoff sleep")

    async def test_quota_exceeded_raises_the_original_exception_unmodified(self):
        # Callers (e.g. modules/eval_recovery.py) rely on catching the exact
        # original exception and reading its message to parse the reset time.
        original = _status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE)

        async def always_quota_exceeded():
            raise original

        with self.assertRaises(anthropic.BadRequestError) as ctx:
            await with_retry(always_quota_exceeded, retries=3, base_delay=0.01)

        self.assertIs(ctx.exception, original)


class TestWithRetryNonRetryable(unittest.IsolatedAsyncioTestCase):
    """Case 3 from the design: auth failures, permission errors, and
    invalid requests fail immediately with no retry and no special
    notification beyond the exception itself."""

    async def test_authentication_failure_fails_immediately_no_retry(self):
        attempts = []

        async def always_auth_error():
            attempts.append(1)
            raise _status_error(anthropic.AuthenticationError, 401)

        with self.assertRaises(anthropic.AuthenticationError):
            await with_retry(always_auth_error, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1)

    async def test_permission_denied_fails_immediately_no_retry(self):
        attempts = []

        async def always_permission_error():
            attempts.append(1)
            raise _status_error(anthropic.PermissionDeniedError, 403)

        with self.assertRaises(anthropic.PermissionDeniedError):
            await with_retry(always_permission_error, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1)

    async def test_generic_invalid_request_fails_immediately_no_retry(self):
        attempts = []

        async def always_invalid_request():
            attempts.append(1)
            raise _status_error(anthropic.BadRequestError, 400, "messages: roles must alternate")

        with self.assertRaises(anthropic.BadRequestError):
            await with_retry(always_invalid_request, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1)

    async def test_non_retryable_never_sleeps(self):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        async def always_auth_error():
            raise _status_error(anthropic.AuthenticationError, 401)

        with patch("modules.util.asyncio.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.AuthenticationError):
                await with_retry(always_auth_error, retries=5, base_delay=2.0)

        self.assertEqual(slept, [], "non-retryable errors must never trigger a backoff sleep")


class TestWithRetrySyncTransientAnd5xx(unittest.TestCase):
    """with_retry_sync mirrors with_retry's transient/5xx behavior exactly -
    see TestWithRetryTransientAnd5xx above - just synchronous."""

    def test_transient_error_retries_then_succeeds(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise _status_error(anthropic.InternalServerError, 503)
            return "success"

        with patch("modules.util.time.sleep"):
            result = with_retry_sync(flaky, retries=3, base_delay=0.01)

        self.assertEqual(result, "success")
        self.assertEqual(len(attempts), 3, "should have taken exactly 3 attempts to succeed")

    def test_transient_error_exhausts_retries_and_raises(self):
        attempts = []

        def always_503():
            attempts.append(1)
            raise _status_error(anthropic.InternalServerError, 503)

        with patch("modules.util.time.sleep"):
            with self.assertRaises(anthropic.InternalServerError):
                with_retry_sync(always_503, retries=3, base_delay=0.01)

        self.assertEqual(len(attempts), 3, "should attempt exactly `retries` times before giving up")

    def test_backoff_delay_doubles_each_retry(self):
        delays = []

        def fake_sleep(seconds):
            delays.append(seconds)

        def always_503():
            raise _status_error(anthropic.InternalServerError, 503)

        with patch("modules.util.time.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.InternalServerError):
                with_retry_sync(always_503, retries=3, base_delay=2.0)

        self.assertEqual(delays, [2.0, 4.0])


class TestWithRetrySyncRateLimited(unittest.TestCase):
    """HTTP 429 retries with exponential backoff, honoring Retry-After when
    present - see TestWithRetryRateLimited above."""

    def test_rate_limited_retries_then_succeeds(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise _status_error(anthropic.RateLimitError, 429, "slow down")
            return "success"

        with patch("modules.util.time.sleep"):
            result = with_retry_sync(flaky, retries=3, base_delay=0.01)

        self.assertEqual(result, "success")
        self.assertEqual(len(attempts), 3)

    def test_rate_limited_without_retry_after_header_uses_exponential_backoff(self):
        delays = []

        def fake_sleep(seconds):
            delays.append(seconds)

        def always_429():
            raise _status_error(anthropic.RateLimitError, 429, "slow down")  # no retry-after header

        with patch("modules.util.time.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.RateLimitError):
                with_retry_sync(always_429, retries=3, base_delay=2.0)

        self.assertEqual(delays, [2.0, 4.0])

    def test_rate_limited_with_retry_after_header_honors_it_instead_of_backoff(self):
        delays = []

        def fake_sleep(seconds):
            delays.append(seconds)

        def always_429():
            raise _status_error(anthropic.RateLimitError, 429, "slow down", headers={"retry-after": "15"})

        with patch("modules.util.time.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.RateLimitError):
                with_retry_sync(always_429, retries=3, base_delay=2.0)

        self.assertEqual(delays, [15.0, 15.0])

    def test_retry_after_value_is_capped_at_max_delay(self):
        delays = []

        def fake_sleep(seconds):
            delays.append(seconds)

        def always_429():
            raise _status_error(anthropic.RateLimitError, 429, "slow down", headers={"retry-after": "99999"})

        with patch("modules.util.time.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.RateLimitError):
                with_retry_sync(always_429, retries=2, base_delay=2.0, max_delay=30.0)

        self.assertEqual(delays, [30.0], "an absurd Retry-After value must be capped, not slept on directly")

    def test_non_numeric_retry_after_falls_back_to_exponential_backoff(self):
        delays = []

        def fake_sleep(seconds):
            delays.append(seconds)

        def always_429():
            raise _status_error(anthropic.RateLimitError, 429, "slow down", headers={"retry-after": "not-a-number"})

        with patch("modules.util.time.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.RateLimitError):
                with_retry_sync(always_429, retries=2, base_delay=2.0)

        self.assertEqual(delays, [2.0])


class TestWithRetrySyncQuotaExceeded(unittest.TestCase):
    """Account quota exhaustion fails fast, is never retried, and the
    exception is re-raised unmodified - see TestWithRetryQuotaExceeded
    above."""

    def test_quota_exceeded_fails_immediately_no_retry(self):
        attempts = []

        def always_quota_exceeded():
            attempts.append(1)
            raise _status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE)

        with self.assertRaises(anthropic.BadRequestError):
            with_retry_sync(always_quota_exceeded, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1, "a quota-exceeded error must not be retried at all")

    def test_quota_exceeded_never_sleeps(self):
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)

        def always_quota_exceeded():
            raise _status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE)

        with patch("modules.util.time.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.BadRequestError):
                with_retry_sync(always_quota_exceeded, retries=5, base_delay=2.0)

        self.assertEqual(slept, [], "quota exhaustion must never trigger a backoff sleep")

    def test_quota_exceeded_raises_the_original_exception_unmodified(self):
        original = _status_error(anthropic.BadRequestError, 400, QUOTA_EXCEEDED_MESSAGE)

        def always_quota_exceeded():
            raise original

        with self.assertRaises(anthropic.BadRequestError) as ctx:
            with_retry_sync(always_quota_exceeded, retries=3, base_delay=0.01)

        self.assertIs(ctx.exception, original)


class TestWithRetrySyncNonRetryable(unittest.TestCase):
    """Auth failures, permission errors, and invalid requests fail
    immediately with no retry - see TestWithRetryNonRetryable above."""

    def test_authentication_failure_fails_immediately_no_retry(self):
        attempts = []

        def always_auth_error():
            attempts.append(1)
            raise _status_error(anthropic.AuthenticationError, 401)

        with self.assertRaises(anthropic.AuthenticationError):
            with_retry_sync(always_auth_error, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1)

    def test_permission_denied_fails_immediately_no_retry(self):
        attempts = []

        def always_permission_error():
            attempts.append(1)
            raise _status_error(anthropic.PermissionDeniedError, 403)

        with self.assertRaises(anthropic.PermissionDeniedError):
            with_retry_sync(always_permission_error, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1)

    def test_generic_invalid_request_fails_immediately_no_retry(self):
        attempts = []

        def always_invalid_request():
            attempts.append(1)
            raise _status_error(anthropic.BadRequestError, 400, "messages: roles must alternate")

        with self.assertRaises(anthropic.BadRequestError):
            with_retry_sync(always_invalid_request, retries=5, base_delay=0.01)

        self.assertEqual(len(attempts), 1)

    def test_non_retryable_never_sleeps(self):
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)

        def always_auth_error():
            raise _status_error(anthropic.AuthenticationError, 401)

        with patch("modules.util.time.sleep", new=fake_sleep):
            with self.assertRaises(anthropic.AuthenticationError):
                with_retry_sync(always_auth_error, retries=5, base_delay=2.0)

        self.assertEqual(slept, [], "non-retryable errors must never trigger a backoff sleep")


if __name__ == "__main__":
    unittest.main()
