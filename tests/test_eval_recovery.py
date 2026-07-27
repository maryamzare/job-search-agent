"""
Tests for modules.eval_recovery.

Covers: parsing the quota-reset time out of Anthropic's error message,
saving/loading recovery state, and the check-and-run state machine
(not ready yet / succeeds / still over quota / unexpected failure) -
all without any real API call or touching real data/outputs files.

Run: python3 -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.eval_recovery import (
    generate_comparison_report,
    load_recovery_state,
    parse_quota_reset_time,
    run_recovery_check,
    save_failed_eval_state,
)

REAL_QUOTA_MESSAGE = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'You have reached your specified API usage limits. "
    "You will regain access on 2026-08-01 at 00:00 UTC.'}, 'request_id': 'req_abc'}"
)


class TestParseQuotaResetTime(unittest.TestCase):
    def test_parses_the_real_anthropic_message_format(self):
        result = parse_quota_reset_time(REAL_QUOTA_MESSAGE)
        self.assertEqual(result, datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc))

    def test_returns_none_for_unrelated_error_text(self):
        self.assertIsNone(parse_quota_reset_time("Connection timed out"))

    def test_returns_none_rather_than_guessing_on_partial_match(self):
        self.assertIsNone(parse_quota_reset_time("You will regain access soon"))


class TestSaveAndLoadRecoveryState(unittest.TestCase):
    def test_roundtrip_preserves_all_fields(self):
        tmp = tempfile.mktemp(suffix=".json")
        target = {"company": "TestCo", "title": "TPM", "resume_path": "outputs/x.txt"}
        before = {"red_flag_verdict": "major-flags", "composite_score": 6.4}

        saved = save_failed_eval_state(target, before, RuntimeError(REAL_QUOTA_MESSAGE), path=tmp)
        loaded = load_recovery_state(tmp)

        self.assertEqual(loaded, saved)
        self.assertEqual(loaded["status"], "pending")
        self.assertEqual(loaded["target"], target)
        self.assertEqual(loaded["before"], before)
        self.assertEqual(loaded["quota_reset_at"], "2026-08-01T00:00:00+00:00")
        self.assertEqual(loaded["attempts"], 1)

    def test_missing_state_file_returns_none(self):
        self.assertIsNone(load_recovery_state("/tmp/definitely_does_not_exist_xyz.json"))

    def test_unparseable_error_leaves_quota_reset_at_none(self):
        tmp = tempfile.mktemp(suffix=".json")
        save_failed_eval_state({"company": "X", "title": "Y"}, {}, RuntimeError("some other error"), path=tmp)
        loaded = load_recovery_state(tmp)
        self.assertIsNone(loaded["quota_reset_at"])


class TestGenerateComparisonReport(unittest.TestCase):
    def test_marks_changed_fields_and_includes_target(self):
        target = {"company": "Acme", "title": "TPM"}
        before = {"verdict": "major-flags", "score": 6}
        after = {"verdict": "clean", "score": 6}

        report = generate_comparison_report(target, before, after)

        self.assertIn("Acme", report)
        self.assertIn("TPM", report)
        self.assertIn("major-flags", report)
        self.assertIn("clean", report)
        # verdict changed -> marked; score unchanged -> not marked
        verdict_line = next(line for line in report.splitlines() if line.startswith("| verdict"))
        score_line = next(line for line in report.splitlines() if line.startswith("| score"))
        self.assertIn("changed", verdict_line)
        self.assertNotIn("changed", score_line)


class TestRunRecoveryCheck(unittest.TestCase):
    def _make_pending_state(self, path, quota_reset_at_iso):
        from modules.util import save_json
        state = {
            "status": "pending",
            "created_at": "2026-07-27T00:00:00+00:00",
            "last_attempt_at": "2026-07-27T00:00:00+00:00",
            "quota_reset_at": quota_reset_at_iso,
            "target": {"company": "Amazon", "title": "TPM", "resume_path": "outputs/x.txt"},
            "before": {"verdict": "major-flags", "composite_score": 6.4},
            "after": None,
            "report_path": None,
            "last_error": REAL_QUOTA_MESSAGE,
            "attempts": 1,
        }
        save_json(state, path)
        return state

    def test_no_state_file_is_a_noop(self):
        exit_code = run_recovery_check(
            state_path="/tmp/no_such_recovery_state_xyz.json",
            live_rerun_fn=lambda target: self.fail("must not be called"),
        )
        self.assertEqual(exit_code, 0)

    def test_not_ready_yet_does_not_call_live_rerun_fn(self):
        tmp = tempfile.mktemp(suffix=".json")
        future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        self._make_pending_state(tmp, future)

        called = []
        exit_code = run_recovery_check(
            state_path=tmp,
            live_rerun_fn=lambda target: called.append(target) or {},
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(called, [], "live_rerun_fn must not be called before the reset time")
        # state must remain pending, untouched
        self.assertEqual(load_recovery_state(tmp)["status"], "pending")

    def test_ready_and_succeeds_writes_report_and_marks_completed(self):
        tmp_state = tempfile.mktemp(suffix=".json")
        tmp_reports = tempfile.mkdtemp()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._make_pending_state(tmp_state, past)

        after_snapshot = {"verdict": "clean", "composite_score": 8.1}
        exit_code = run_recovery_check(
            state_path=tmp_state,
            live_rerun_fn=lambda target: after_snapshot,
            reports_dir=tmp_reports,
        )

        self.assertEqual(exit_code, 0)
        state = load_recovery_state(tmp_state)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["after"], after_snapshot)
        self.assertIsNotNone(state["report_path"])
        with open(state["report_path"]) as f:
            report_text = f.read()
        self.assertIn("clean", report_text)
        self.assertIn("major-flags", report_text)

    def test_still_over_quota_updates_reset_time_and_stays_pending(self):
        tmp_state = tempfile.mktemp(suffix=".json")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._make_pending_state(tmp_state, past)

        new_quota_message = REAL_QUOTA_MESSAGE.replace("2026-08-01", "2026-09-01")

        def still_blocked(target):
            raise RuntimeError(new_quota_message)

        exit_code = run_recovery_check(state_path=tmp_state, live_rerun_fn=still_blocked)

        self.assertEqual(exit_code, 0, "still-over-quota is not a failure, just needs to wait longer")
        state = load_recovery_state(tmp_state)
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["quota_reset_at"], "2026-09-01T00:00:00+00:00")

    def test_unexpected_error_marks_failed_and_returns_nonzero(self):
        tmp_state = tempfile.mktemp(suffix=".json")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self._make_pending_state(tmp_state, past)

        def broken(target):
            raise ValueError("something unrelated to quota broke")

        exit_code = run_recovery_check(state_path=tmp_state, live_rerun_fn=broken)

        self.assertEqual(exit_code, 1, "an unexpected failure should signal for human attention")
        state = load_recovery_state(tmp_state)
        self.assertEqual(state["status"], "failed")

    def test_already_completed_is_a_noop(self):
        tmp_state = tempfile.mktemp(suffix=".json")
        from modules.util import save_json
        save_json({"status": "completed", "report_path": "outputs/eval_reports/x.md"}, tmp_state)

        called = []
        exit_code = run_recovery_check(
            state_path=tmp_state,
            live_rerun_fn=lambda target: called.append(target) or {},
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(called, [])

    def test_already_failed_returns_nonzero_without_retrying(self):
        tmp_state = tempfile.mktemp(suffix=".json")
        from modules.util import save_json
        save_json({"status": "failed", "last_error": "some bug"}, tmp_state)

        called = []
        exit_code = run_recovery_check(
            state_path=tmp_state,
            live_rerun_fn=lambda target: called.append(target) or {},
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
