"""
Tests for module6_tracker's queue organization: the actionable/history
grouping views and the status-transition timestamps that back them.

No real file I/O - load_queue/save_queue are patched on the module
throughout, following the same pattern as test_v2_throughput_fixes.py.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modules.module6_tracker as tracker


def _job(company, title, status, **extra):
    return {"company": company, "title": title, "status": status, "fit_score": 80, **extra}


class TestActionableGroups(unittest.TestCase):
    def test_ready_to_apply_includes_shortlisted_and_board_approved(self):
        jobs = [
            _job("A", "TPM", "shortlisted"),
            _job("B", "TPM", "board_approved"),
        ]
        groups = tracker.actionable_groups(jobs)
        self.assertEqual({j["company"] for j in groups["Ready to apply"]}, {"A", "B"})
        self.assertEqual(groups["In progress"], [])

    def test_in_progress_grouped_separately(self):
        jobs = [_job("A", "TPM", "in_progress")]
        groups = tracker.actionable_groups(jobs)
        self.assertEqual([j["company"] for j in groups["In progress"]], ["A"])
        self.assertEqual(groups["Ready to apply"], [])

    def test_non_actionable_statuses_excluded_entirely(self):
        jobs = [
            _job("A", "TPM", "discovered"),
            _job("B", "TPM", "filtered_out"),
            _job("C", "TPM", "applied"),
            _job("D", "TPM", "rejected"),
            _job("E", "TPM", "closed"),
            _job("F", "TPM", "interviewing"),
            _job("G", "TPM", "offer"),
        ]
        groups = tracker.actionable_groups(jobs)
        all_grouped = [j for jobs_ in groups.values() for j in jobs_]
        self.assertEqual(all_grouped, [])

    def test_every_job_lands_in_exactly_one_group(self):
        jobs = [
            _job("A", "TPM", "shortlisted"),
            _job("B", "TPM", "board_approved"),
            _job("C", "TPM", "in_progress"),
        ]
        groups = tracker.actionable_groups(jobs)
        seen = [j["company"] for jobs_ in groups.values() for j in jobs_]
        self.assertEqual(sorted(seen), ["A", "B", "C"])


class TestHistoryGroups(unittest.TestCase):
    def test_all_five_groups_populated_correctly(self):
        jobs = [
            _job("A", "TPM", "applied"),
            _job("B", "TPM", "interviewing"),
            _job("C", "TPM", "questionnaire_submitted"),
            _job("D", "TPM", "rejected"),
            _job("E", "TPM", "closed"),
            _job("F", "TPM", "offer"),
        ]
        groups = tracker.history_groups(jobs)
        self.assertEqual([j["company"] for j in groups["Applied / Waiting"]], ["A"])
        self.assertEqual({j["company"] for j in groups["Interviewing"]}, {"B", "C"})
        self.assertEqual([j["company"] for j in groups["Rejected"]], ["D"])
        self.assertEqual([j["company"] for j in groups["Closed"]], ["E"])
        self.assertEqual([j["company"] for j in groups["Offers"]], ["F"])

    def test_pre_application_statuses_excluded(self):
        jobs = [
            _job("A", "TPM", "discovered"),
            _job("B", "TPM", "shortlisted"),
            _job("C", "TPM", "board_approved"),
            _job("D", "TPM", "in_progress"),
            _job("E", "TPM", "filtered_out"),
        ]
        groups = tracker.history_groups(jobs)
        all_grouped = [j for jobs_ in groups.values() for j in jobs_]
        self.assertEqual(all_grouped, [])


class TestUpdateStatusTransitionsAndTimestamps(unittest.TestCase):
    def _run_update(self, jobs, company, title, new_status):
        queue = {"jobs": jobs}
        with patch.object(tracker, "load_queue", return_value=queue), \
             patch.object(tracker, "save_queue") as save:
            tracker.update_status(company, title, new_status)
        return queue["jobs"][0], save

    def test_applied_sets_applied_date_and_submitted_at(self):
        job = _job("Acme", "TPM", "shortlisted")
        updated, save = self._run_update([job], "Acme", "TPM", "applied")
        self.assertEqual(updated["status"], "applied")
        self.assertIsNotNone(updated.get("applied_date"))
        self.assertIsNotNone(updated.get("application_submitted_at"))
        save.assert_called_once()

    def test_interviewing_sets_interviewing_at_and_backfills_applied(self):
        job = _job("Acme", "TPM", "applied")
        updated, _ = self._run_update([job], "Acme", "TPM", "interviewing")
        self.assertEqual(updated["status"], "interviewing")
        self.assertIsNotNone(updated.get("interviewing_at"))
        self.assertIsNotNone(updated.get("applied_date"))

    def test_questionnaire_submitted_sets_its_own_timestamp(self):
        job = _job("Acme", "TPM", "interviewing")
        updated, _ = self._run_update([job], "Acme", "TPM", "questionnaire_submitted")
        self.assertIsNotNone(updated.get("questionnaire_submitted_at"))

    def test_offer_sets_offer_at(self):
        job = _job("Acme", "TPM", "interviewing")
        updated, _ = self._run_update([job], "Acme", "TPM", "offer")
        self.assertIsNotNone(updated.get("offer_at"))

    def test_rejected_sets_rejected_at(self):
        job = _job("Acme", "TPM", "shortlisted")
        updated, _ = self._run_update([job], "Acme", "TPM", "rejected")
        self.assertIsNotNone(updated.get("rejected_at"))
        # Rejected without ever passing through "applied" still backfills
        # the applied markers, per lifecycle_metrics' fallback contract.
        self.assertIsNotNone(updated.get("applied_date"))

    def test_closed_sets_closed_or_expired_at(self):
        job = _job("Acme", "TPM", "shortlisted")
        updated, _ = self._run_update([job], "Acme", "TPM", "closed")
        self.assertIsNotNone(updated.get("closed_or_expired_at"))
        # "closed" is not a post-application status - no applied backfill.
        self.assertIsNone(updated.get("applied_date"))

    def test_repeat_transition_does_not_overwrite_existing_timestamp(self):
        job = _job("Acme", "TPM", "applied", applied_date="2026-01-01",
                   application_submitted_at="2026-01-01T00:00:00+00:00")
        updated, _ = self._run_update([job], "Acme", "TPM", "applied")
        self.assertEqual(updated["applied_date"], "2026-01-01")
        self.assertEqual(updated["application_submitted_at"], "2026-01-01T00:00:00+00:00")

    def test_invalid_status_leaves_job_unchanged(self):
        job = _job("Acme", "TPM", "shortlisted")
        queue = {"jobs": [job]}
        with patch.object(tracker, "load_queue", return_value=queue), \
             patch.object(tracker, "save_queue") as save:
            tracker.update_status("Acme", "TPM", "not_a_real_status")
        self.assertEqual(job["status"], "shortlisted")
        save.assert_not_called()

    def test_job_not_found_does_not_crash_or_save(self):
        queue = {"jobs": [_job("Acme", "TPM", "shortlisted")]}
        with patch.object(tracker, "load_queue", return_value=queue), \
             patch.object(tracker, "save_queue") as save:
            tracker.update_status("Nobody", "Nothing", "applied")
        save.assert_not_called()

    def test_match_is_case_insensitive(self):
        job = _job("Acme", "TPM", "shortlisted")
        updated, _ = self._run_update([job], "acme", "tpm", "applied")
        self.assertEqual(updated["status"], "applied")


class TestUpdateStatusNotesCompatibility(unittest.TestCase):
    """"notes" predates the list-based schema on some older queue entries,
    where it's a single string rather than a list. update_status must
    normalize any shape to a list without ever discarding what was there."""

    def _run_update(self, jobs, company, title, new_status):
        queue = {"jobs": jobs}
        with patch.object(tracker, "load_queue", return_value=queue), \
             patch.object(tracker, "save_queue") as save:
            tracker.update_status(company, title, new_status)
        return queue["jobs"][0], save

    def _run_update_with_note(self, job, note_text):
        queue = {"jobs": [job]}
        with patch.object(tracker, "load_queue", return_value=queue), \
             patch.object(tracker, "save_queue") as save:
            tracker.update_status("Acme", "TPM", "applied", notes=note_text)
        return queue["jobs"][0], save

    def test_existing_string_note_is_preserved_and_converted_to_a_list(self):
        job = _job("Acme", "TPM", "shortlisted", notes="Sourced via LinkedIn 2026-01-01.")
        updated, save = self._run_update_with_note(job, "Applied today.")
        self.assertIsInstance(updated["notes"], list)
        self.assertEqual(updated["notes"][0], "Sourced via LinkedIn 2026-01-01.")
        self.assertTrue(updated["notes"][1].endswith("Applied today."))
        save.assert_called_once()

    def test_existing_list_note_is_preserved_unchanged_and_appended_to(self):
        job = _job("Acme", "TPM", "shortlisted", notes=["2026-01-01: Sourced via LinkedIn."])
        updated, _ = self._run_update_with_note(job, "Applied today.")
        self.assertEqual(len(updated["notes"]), 2)
        self.assertEqual(updated["notes"][0], "2026-01-01: Sourced via LinkedIn.")
        self.assertTrue(updated["notes"][1].endswith("Applied today."))

    def test_null_notes_becomes_a_list(self):
        job = _job("Acme", "TPM", "shortlisted", notes=None)
        updated, _ = self._run_update_with_note(job, "Applied today.")
        self.assertIsInstance(updated["notes"], list)
        self.assertEqual(len(updated["notes"]), 1)
        self.assertTrue(updated["notes"][0].endswith("Applied today."))

    def test_missing_notes_key_becomes_a_list(self):
        job = _job("Acme", "TPM", "shortlisted")  # no "notes" key at all
        self.assertNotIn("notes", job)
        updated, _ = self._run_update_with_note(job, "Applied today.")
        self.assertIsInstance(updated["notes"], list)
        self.assertEqual(len(updated["notes"]), 1)
        self.assertTrue(updated["notes"][0].endswith("Applied today."))

    def test_no_notes_argument_does_not_touch_the_notes_field(self):
        job = _job("Acme", "TPM", "shortlisted", notes="Sourced via LinkedIn.")
        updated, _ = self._run_update([job], "Acme", "TPM", "applied")
        self.assertEqual(updated["notes"], "Sourced via LinkedIn.")  # untouched, not normalized


if __name__ == "__main__":
    unittest.main()
