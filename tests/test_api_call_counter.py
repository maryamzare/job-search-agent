"""
Tests for modules.util's running API-call counter: every real call to
client.messages.create() - via tracked_create or tracked_create_async,
success or failure, including each individual retry attempt - increments
and prints util.api_call_count exactly once.

No real API calls - client.messages.create is mocked throughout.

Run: python3 -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modules.util as util


class TestApiCallCounter(unittest.TestCase):
    def setUp(self):
        self._original_count = util.api_call_count
        util.api_call_count = 0
        self._original_log_path = util.LLM_USAGE_LOG_PATH
        util.LLM_USAGE_LOG_PATH = tempfile.mktemp(suffix=".jsonl")

    def tearDown(self):
        util.api_call_count = self._original_count
        util.LLM_USAGE_LOG_PATH = self._original_log_path

    def _fake_client(self, response=None, exc=None):
        client = MagicMock()
        if exc is not None:
            client.messages.create.side_effect = exc
        else:
            client.messages.create.return_value = response
        return client

    def _fake_response(self):
        response = MagicMock()
        response.content = [MagicMock(text="ok")]
        response.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        return response

    def test_starts_at_zero(self):
        self.assertEqual(util.api_call_count, 0)

    def test_successful_call_increments_by_one(self):
        client = self._fake_client(response=self._fake_response())
        util.tracked_create(client, "score_job", model="claude-sonnet-4-6", max_tokens=10, messages=[])
        self.assertEqual(util.api_call_count, 1)

    def test_failed_call_still_increments(self):
        client = self._fake_client(exc=ValueError("boom"))
        with self.assertRaises(ValueError):
            util.tracked_create(client, "score_job", model="claude-sonnet-4-6", max_tokens=10, messages=[])
        self.assertEqual(util.api_call_count, 1, "a failed attempt is still a real API call")

    def test_multiple_calls_accumulate(self):
        client = self._fake_client(response=self._fake_response())
        for _ in range(3):
            util.tracked_create(client, "score_job", model="claude-sonnet-4-6", max_tokens=10, messages=[])
        self.assertEqual(util.api_call_count, 3)

    def test_each_retry_attempt_counts_separately(self):
        # Two failed attempts + one success = 3 real calls, even though the
        # caller only sees one (successful) return value.
        client = MagicMock()
        client.messages.create.side_effect = [ValueError("boom"), ValueError("boom"), self._fake_response()]

        def _call():
            return util.tracked_create(client, "score_job", model="claude-sonnet-4-6", max_tokens=10, messages=[])

        attempts = 0
        for _ in range(3):
            try:
                _call()
                break
            except ValueError:
                attempts += 1
                continue
        self.assertEqual(util.api_call_count, 3)


class TestApiCallCounterAsync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._original_count = util.api_call_count
        util.api_call_count = 0
        self._original_log_path = util.LLM_USAGE_LOG_PATH
        util.LLM_USAGE_LOG_PATH = tempfile.mktemp(suffix=".jsonl")

    async def asyncTearDown(self):
        util.api_call_count = self._original_count
        util.LLM_USAGE_LOG_PATH = self._original_log_path

    async def test_async_call_increments_too(self):
        response = MagicMock()
        response.content = [MagicMock(text="ok")]
        response.usage = MagicMock(
            input_tokens=10, output_tokens=5,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )

        async def _create(**kwargs):
            return response

        client = MagicMock()
        client.messages.create = _create

        await util.tracked_create_async(client, "board_review:chair", model="claude-sonnet-4-6", max_tokens=10, messages=[])
        self.assertEqual(util.api_call_count, 1)


if __name__ == "__main__":
    unittest.main()
