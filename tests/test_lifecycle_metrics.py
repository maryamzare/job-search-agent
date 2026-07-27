"""
Tests for modules.lifecycle_metrics: timestamp handling, metric
calculations, and missing-data scenarios.

Run: python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.lifecycle_metrics import compute_metrics, generate_report, hours_between

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _job(**overrides) -> dict:
    job = {
        "company": "TestCo",
        "title": "TPM",
        "status": "shortlisted",
        "fit_score": 75,
    }
    job.update(overrides)
    return job


class TestHoursBetween(unittest.TestCase):
    """Timestamp handling: correct math, and graceful (never-raising)
    handling of missing/malformed values."""

    def test_computes_correct_hours_for_two_real_timestamps(self):
        start = _iso(NOW)
        end = _iso(NOW + timedelta(hours=36))
        self.assertEqual(hours_between(start, end), 36.0)

    def test_handles_fractional_hours(self):
        start = _iso(NOW)
        end = _iso(NOW + timedelta(hours=1, minutes=30))
        self.assertEqual(hours_between(start, end), 1.5)

    def test_none_start_returns_none(self):
        self.assertIsNone(hours_between(None, _iso(NOW)))

    def test_none_end_returns_none(self):
        self.assertIsNone(hours_between(_iso(NOW), None))

    def test_both_none_returns_none(self):
        self.assertIsNone(hours_between(None, None))

    def test_malformed_start_string_returns_none_not_raise(self):
        self.assertIsNone(hours_between("not-a-timestamp", _iso(NOW)))

    def test_malformed_end_string_returns_none_not_raise(self):
        self.assertIsNone(hours_between(_iso(NOW), "not-a-timestamp"))

    def test_empty_string_treated_as_missing(self):
        self.assertIsNone(hours_between("", _iso(NOW)))

    def test_handles_z_suffixed_timestamps(self):
        # Anthropic/other external data may use "Z" instead of "+00:00" -
        # both should parse.
        self.assertEqual(hours_between("2026-07-27T12:00:00Z", "2026-07-28T12:00:00Z"), 24.0)

    def test_naive_timestamp_assumed_utc_not_raise(self):
        # A timestamp with no timezone info shouldn't crash the comparison.
        result = hours_between("2026-07-27T12:00:00", "2026-07-28T12:00:00")
        self.assertEqual(result, 24.0)


class TestComputeMetricsDurations(unittest.TestCase):
    """Average/median shortlist-to-apply time calculations."""

    def test_average_and_median_over_known_values(self):
        jobs = [
            _job(shortlisted_at=_iso(NOW), application_submitted_at=_iso(NOW + timedelta(hours=10)),
                 status="applied"),
            _job(shortlisted_at=_iso(NOW), application_submitted_at=_iso(NOW + timedelta(hours=20)),
                 status="applied"),
            _job(shortlisted_at=_iso(NOW), application_submitted_at=_iso(NOW + timedelta(hours=30)),
                 status="applied"),
        ]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["jobs_with_both_timestamps"], 3)
        self.assertEqual(metrics["average_shortlist_to_apply_hours"], 20.0)
        self.assertEqual(metrics["median_shortlist_to_apply_hours"], 20.0)

    def test_median_with_even_count_averages_middle_two(self):
        jobs = [
            _job(shortlisted_at=_iso(NOW), application_submitted_at=_iso(NOW + timedelta(hours=10)),
                 status="applied"),
            _job(shortlisted_at=_iso(NOW), application_submitted_at=_iso(NOW + timedelta(hours=20)),
                 status="applied"),
        ]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["median_shortlist_to_apply_hours"], 15.0)

    def test_no_jobs_with_both_timestamps_yields_none_not_zero(self):
        jobs = [_job(status="shortlisted", shortlisted_at=_iso(NOW))]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["jobs_with_both_timestamps"], 0)
        self.assertIsNone(metrics["average_shortlist_to_apply_hours"])
        self.assertIsNone(metrics["median_shortlist_to_apply_hours"])


class TestComputeMetricsNeverApplied(unittest.TestCase):
    def test_pending_statuses_count_as_never_applied(self):
        jobs = [
            _job(status="shortlisted"),
            _job(status="board_approved"),
            _job(status="in_progress"),
        ]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["never_applied_count"], 3)

    def test_post_application_statuses_do_not_count_as_never_applied(self):
        # These predate application_submitted_at existing as a field, but
        # their status alone proves an application happened.
        jobs = [
            _job(status="applied"),
            _job(status="interviewing"),
            _job(status="rejected"),
            _job(status="offer"),
            _job(status="questionnaire_submitted"),
        ]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["never_applied_count"], 0)

    def test_filtered_out_and_discovered_do_not_count(self):
        jobs = [_job(status="filtered_out"), _job(status="discovered")]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["never_applied_count"], 0)

    def test_never_applied_jobs_sorted_by_fit_score_descending(self):
        jobs = [
            _job(status="shortlisted", fit_score=60, company="Low"),
            _job(status="shortlisted", fit_score=90, company="High"),
            _job(status="shortlisted", fit_score=75, company="Mid"),
        ]
        metrics = compute_metrics(jobs, now=NOW)
        companies = [j["company"] for j in metrics["never_applied_jobs"]]
        self.assertEqual(companies, ["High", "Mid", "Low"])


class TestComputeMetricsClosedBeforeApplication(unittest.TestCase):
    def test_closed_without_application_counts(self):
        jobs = [_job(status="closed", application_submitted_at=None)]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["closed_before_application_count"], 1)

    def test_closed_after_application_does_not_count(self):
        # Edge case: a job could be marked closed after already applying
        # (e.g. the posting was taken down post-application) - that's not
        # a loss, so it shouldn't be flagged.
        jobs = [_job(status="closed", application_submitted_at=_iso(NOW))]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["closed_before_application_count"], 0)

    def test_non_closed_statuses_never_count(self):
        jobs = [_job(status="shortlisted"), _job(status="applied")]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["closed_before_application_count"], 0)

    def test_works_without_shortlisted_at_present(self):
        # Legacy jobs predate shortlisted_at entirely - the metric must
        # still work since it's status-driven, not timestamp-driven.
        jobs = [_job(status="closed", shortlisted_at=None, application_submitted_at=None)]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["closed_before_application_count"], 1)


class TestComputeMetricsHighScoreDelayed(unittest.TestCase):
    def test_still_pending_high_score_job_past_threshold_is_flagged(self):
        jobs = [_job(
            status="shortlisted", fit_score=85,
            shortlisted_at=_iso(NOW - timedelta(hours=72)),
        )]
        metrics = compute_metrics(jobs, high_score_threshold=80, delay_threshold_hours=48, now=NOW)
        self.assertEqual(metrics["high_score_delayed_count"], 1)
        self.assertEqual(metrics["high_score_delayed_jobs"][0]["reason"], "still_pending")

    def test_still_pending_high_score_job_within_threshold_is_not_flagged(self):
        jobs = [_job(
            status="shortlisted", fit_score=85,
            shortlisted_at=_iso(NOW - timedelta(hours=10)),
        )]
        metrics = compute_metrics(jobs, high_score_threshold=80, delay_threshold_hours=48, now=NOW)
        self.assertEqual(metrics["high_score_delayed_count"], 0)

    def test_low_score_pending_job_never_flagged_regardless_of_delay(self):
        jobs = [_job(
            status="shortlisted", fit_score=65,
            shortlisted_at=_iso(NOW - timedelta(hours=200)),
        )]
        metrics = compute_metrics(jobs, high_score_threshold=80, delay_threshold_hours=48, now=NOW)
        self.assertEqual(metrics["high_score_delayed_count"], 0)

    def test_high_score_job_that_applied_late_is_flagged(self):
        jobs = [_job(
            status="applied", fit_score=90,
            shortlisted_at=_iso(NOW),
            application_submitted_at=_iso(NOW + timedelta(hours=96)),
        )]
        metrics = compute_metrics(jobs, high_score_threshold=80, delay_threshold_hours=48, now=NOW)
        self.assertEqual(metrics["high_score_delayed_count"], 1)
        self.assertEqual(metrics["high_score_delayed_jobs"][0]["reason"], "applied_late")

    def test_high_score_job_applied_promptly_not_flagged(self):
        jobs = [_job(
            status="applied", fit_score=90,
            shortlisted_at=_iso(NOW),
            application_submitted_at=_iso(NOW + timedelta(hours=12)),
        )]
        metrics = compute_metrics(jobs, high_score_threshold=80, delay_threshold_hours=48, now=NOW)
        self.assertEqual(metrics["high_score_delayed_count"], 0)


class TestComputeMetricsMissingData(unittest.TestCase):
    """Missing-data scenarios: jobs with no timestamps at all, partial
    timestamps, missing fit_score, and an empty job list - none of these
    should raise."""

    def test_empty_job_list(self):
        metrics = compute_metrics([], now=NOW)
        self.assertEqual(metrics["jobs_with_both_timestamps"], 0)
        self.assertEqual(metrics["never_applied_count"], 0)
        self.assertEqual(metrics["closed_before_application_count"], 0)
        self.assertEqual(metrics["high_score_delayed_count"], 0)
        self.assertIsNone(metrics["average_shortlist_to_apply_hours"])

    def test_job_with_no_timestamp_fields_at_all(self):
        jobs = [{"company": "X", "title": "Y", "status": "shortlisted", "fit_score": 90}]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["never_applied_count"], 1)
        self.assertEqual(metrics["jobs_with_both_timestamps"], 0)

    def test_job_missing_fit_score_does_not_crash_high_score_check(self):
        jobs = [_job(status="shortlisted", fit_score=None, shortlisted_at=_iso(NOW - timedelta(hours=200)))]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["high_score_delayed_count"], 0)
        self.assertEqual(metrics["never_applied_count"], 1)

    def test_job_missing_status_field_entirely(self):
        jobs = [{"company": "X", "title": "Y"}]
        metrics = compute_metrics(jobs, now=NOW)  # must not raise
        self.assertEqual(metrics["never_applied_count"], 0)
        self.assertEqual(metrics["closed_before_application_count"], 0)

    def test_mixed_batch_of_complete_and_incomplete_jobs(self):
        jobs = [
            _job(status="applied", shortlisted_at=_iso(NOW), application_submitted_at=_iso(NOW + timedelta(hours=5))),
            _job(status="shortlisted", shortlisted_at=None),  # legacy, no timestamp
            _job(status="closed", application_submitted_at=None),
            {"company": "NoFields"},  # missing almost everything
        ]
        metrics = compute_metrics(jobs, now=NOW)
        self.assertEqual(metrics["jobs_with_both_timestamps"], 1)
        self.assertEqual(metrics["never_applied_count"], 1)
        self.assertEqual(metrics["closed_before_application_count"], 1)


class TestGenerateReport(unittest.TestCase):
    def test_report_includes_no_data_yet_message_when_empty(self):
        metrics = compute_metrics([], now=NOW)
        report = generate_report(metrics)
        self.assertIn("No jobs yet have both", report)

    def test_report_includes_summary_table_values(self):
        jobs = [_job(status="applied", shortlisted_at=_iso(NOW), application_submitted_at=_iso(NOW + timedelta(hours=10)))]
        metrics = compute_metrics(jobs, now=NOW)
        report = generate_report(metrics)
        self.assertIn("10.0h", report)
        self.assertIn("Pipeline Timing Report", report)

    def test_report_lists_high_score_delayed_jobs_by_name(self):
        jobs = [_job(
            company="Acme", title="Sr TPM", status="shortlisted", fit_score=90,
            shortlisted_at=_iso(NOW - timedelta(hours=100)),
        )]
        metrics = compute_metrics(jobs, now=NOW)
        report = generate_report(metrics)
        self.assertIn("Acme", report)
        self.assertIn("Sr TPM", report)

    def test_healthy_queue_reports_no_issues(self):
        jobs = [_job(
            status="applied", fit_score=90,
            shortlisted_at=_iso(NOW), application_submitted_at=_iso(NOW + timedelta(hours=5)),
        )]
        metrics = compute_metrics(jobs, now=NOW)
        report = generate_report(metrics)
        self.assertIn("healthy", report)


if __name__ == "__main__":
    unittest.main()
