"""
Tests for the performance-baseline instrumentation: modules.util.track_stage
(the writer) and modules.performance_baseline (the aggregation/reporting
logic). No real API calls anywhere - success/failure paths are simulated.

Run: python3 -m unittest discover -s tests -v
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modules.util as util
from modules.performance_baseline import (
    compute_api_usage_by_module,
    compute_stage_timing_by_module,
    module_for_label,
    rough_token_estimate,
    slowest_modules,
)


class TestTrackStage(unittest.TestCase):
    def setUp(self):
        self._original_path = util.PIPELINE_STAGE_LOG_PATH
        self.tmp_log = tempfile.mktemp(suffix=".jsonl")
        util.PIPELINE_STAGE_LOG_PATH = self.tmp_log

    def tearDown(self):
        util.PIPELINE_STAGE_LOG_PATH = self._original_path

    def _read_entries(self):
        import json
        try:
            with open(self.tmp_log) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def test_successful_block_logs_success_true_and_a_duration(self):
        with util.track_stage("test_stage"):
            time.sleep(0.01)

        entries = self._read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["stage"], "test_stage")
        self.assertTrue(entries[0]["success"])
        self.assertGreaterEqual(entries[0]["duration_s"], 0.01)

    def test_failing_block_logs_success_false_and_reraises(self):
        with self.assertRaises(ValueError):
            with util.track_stage("test_stage"):
                raise ValueError("boom")

        entries = self._read_entries()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["success"])

    def test_extra_kwargs_are_merged_into_the_logged_entry(self):
        with util.track_stage("test_stage", company="Acme", title="TPM"):
            pass

        entries = self._read_entries()
        self.assertEqual(entries[0]["company"], "Acme")
        self.assertEqual(entries[0]["title"], "TPM")

    def test_return_value_of_wrapped_code_is_unaffected(self):
        # track_stage wraps a `with` block, not a function - but confirm a
        # value computed and used inside the block is unaffected by being
        # inside the context manager.
        result = None
        with util.track_stage("test_stage"):
            result = 1 + 1
        self.assertEqual(result, 2)

    def test_multiple_calls_append_rather_than_overwrite(self):
        with util.track_stage("stage_a"):
            pass
        with util.track_stage("stage_b"):
            pass

        entries = self._read_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual([e["stage"] for e in entries], ["stage_a", "stage_b"])


class TestModuleForLabel(unittest.TestCase):
    def test_known_single_call_labels(self):
        self.assertEqual(module_for_label("score_job"), "module2_scoring")
        self.assertEqual(module_for_label("tailor_resume"), "module3_resume")
        self.assertEqual(module_for_label("generate_cover_letter"), "module4_coverletter")

    def test_board_review_prefixed_labels(self):
        self.assertEqual(module_for_label("board_review:chair"), "module2b_board_review")
        self.assertEqual(module_for_label("board_review:fit_reviewer"), "module2b_board_review")

    def test_resume_board_prefixed_labels(self):
        self.assertEqual(module_for_label("resume_board:ats_checker"), "module3b_resume_board")
        self.assertEqual(module_for_label("resume_board:chief_editor"), "module3b_resume_board")
        self.assertEqual(module_for_label("resume_board:rewrite"), "module3b_resume_board")

    def test_unrecognized_label_maps_to_unknown_not_a_crash(self):
        self.assertEqual(module_for_label("something_new"), "unknown")

    def test_empty_label(self):
        self.assertEqual(module_for_label(""), "unknown")


class TestComputeApiUsageByModule(unittest.TestCase):
    def test_aggregates_tokens_and_cost_correctly(self):
        entries = [
            {"label": "score_job", "success": True, "input_tokens": 1000, "output_tokens": 100, "cost_usd": 0.0045},
            {"label": "score_job", "success": True, "input_tokens": 900, "output_tokens": 80, "cost_usd": 0.0039},
        ]
        usage = compute_api_usage_by_module(entries)
        self.assertEqual(usage["module2_scoring"]["calls"], 2)
        self.assertEqual(usage["module2_scoring"]["input_tokens"], 1900)
        self.assertEqual(usage["module2_scoring"]["output_tokens"], 180)
        self.assertEqual(usage["module2_scoring"]["total_tokens"], 2080)
        self.assertAlmostEqual(usage["module2_scoring"]["cost_usd"], 0.0084)

    def test_failed_calls_count_but_contribute_zero_tokens_and_cost(self):
        entries = [{"label": "score_job", "success": False, "error": "boom"}]
        usage = compute_api_usage_by_module(entries)
        self.assertEqual(usage["module2_scoring"]["calls"], 1)
        self.assertEqual(usage["module2_scoring"]["failures"], 1)
        self.assertEqual(usage["module2_scoring"]["total_tokens"], 0)
        self.assertEqual(usage["module2_scoring"]["cost_usd"], 0.0)

    def test_groups_multiple_reviewer_labels_under_one_module(self):
        entries = [
            {"label": "resume_board:ats_checker", "success": True, "input_tokens": 100, "output_tokens": 20, "cost_usd": 0.0006},
            {"label": "resume_board:impact_reviewer", "success": True, "input_tokens": 100, "output_tokens": 20, "cost_usd": 0.0006},
            {"label": "resume_board:chief_editor", "success": True, "input_tokens": 100, "output_tokens": 20, "cost_usd": 0.0006},
        ]
        usage = compute_api_usage_by_module(entries)
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage["module3b_resume_board"]["calls"], 3)

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(compute_api_usage_by_module([]), {})

    def test_missing_token_fields_treated_as_zero_not_a_crash(self):
        entries = [{"label": "score_job", "success": True}]  # no token/cost fields at all
        usage = compute_api_usage_by_module(entries)
        self.assertEqual(usage["module2_scoring"]["total_tokens"], 0)


class TestComputeStageTimingByModule(unittest.TestCase):
    def test_average_and_median_over_known_values(self):
        entries = [
            {"stage": "module2_scoring", "duration_s": 1.0, "success": True},
            {"stage": "module2_scoring", "duration_s": 3.0, "success": True},
        ]
        timing = compute_stage_timing_by_module(entries)
        self.assertEqual(timing["module2_scoring"]["runs"], 2)
        self.assertEqual(timing["module2_scoring"]["avg_duration_s"], 2.0)
        self.assertEqual(timing["module2_scoring"]["median_duration_s"], 2.0)
        self.assertEqual(timing["module2_scoring"]["total_duration_s"], 4.0)

    def test_failures_counted_separately_from_runs(self):
        entries = [
            {"stage": "module3_resume", "duration_s": 1.0, "success": True},
            {"stage": "module3_resume", "duration_s": 0.5, "success": False},
        ]
        timing = compute_stage_timing_by_module(entries)
        self.assertEqual(timing["module3_resume"]["runs"], 2)
        self.assertEqual(timing["module3_resume"]["failures"], 1)

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(compute_stage_timing_by_module([]), {})

    def test_missing_duration_defaults_to_zero_not_a_crash(self):
        entries = [{"stage": "module2_scoring", "success": True}]  # no duration_s field
        timing = compute_stage_timing_by_module(entries)
        self.assertEqual(timing["module2_scoring"]["avg_duration_s"], 0.0)


class TestSlowestModules(unittest.TestCase):
    def test_sorts_slowest_first(self):
        timing = {
            "fast_module": {"avg_duration_s": 1.0},
            "slow_module": {"avg_duration_s": 10.0},
            "medium_module": {"avg_duration_s": 5.0},
        }
        self.assertEqual(slowest_modules(timing), ["slow_module", "medium_module", "fast_module"])

    def test_empty_summary_returns_empty_list(self):
        self.assertEqual(slowest_modules({}), [])

    def test_module_with_no_average_sorts_last_not_a_crash(self):
        timing = {
            "has_data": {"avg_duration_s": 2.0},
            "no_data": {"avg_duration_s": None},
        }
        self.assertEqual(slowest_modules(timing), ["has_data", "no_data"])


class TestRoughTokenEstimate(unittest.TestCase):
    def test_known_length_approximation(self):
        self.assertEqual(rough_token_estimate("a" * 400), 100)

    def test_empty_string_is_zero(self):
        self.assertEqual(rough_token_estimate(""), 0)

    def test_short_string_rounds_down_to_zero(self):
        self.assertEqual(rough_token_estimate("hi"), 0)


if __name__ == "__main__":
    unittest.main()
