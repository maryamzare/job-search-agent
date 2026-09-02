"""
Module 6 — Application Tracker
Displays the current state of job_queue.json and allows status updates.
Statuses: discovered → shortlisted → board_approved | filtered_out → applied
→ in_progress | interviewing → questionnaire_submitted → offer | closed | rejected
"""

from datetime import date, datetime, timezone
from config import JOB_QUEUE_PATH
from modules.util import load_queue as _load_queue, save_queue as _save_queue

VALID_STATUSES = [
    "discovered",
    "shortlisted",
    "board_approved",
    "filtered_out",
    "applied",
    "in_progress",
    "interviewing",
    "questionnaire_submitted",
    "offer",
    "closed",
    "rejected",
]

STATUS_EMOJI = {
    "discovered": "🔍",
    "shortlisted": "⭐",
    "board_approved": "✅",
    "filtered_out": "✗",
    "applied": "📤",
    "in_progress": "✍️",
    "interviewing": "🗣",
    "questionnaire_submitted": "📝",
    "offer": "🎉",
    "closed": "🔒",
    "rejected": "✗",
}


def load_queue() -> dict:
    return _load_queue(JOB_QUEUE_PATH)


def save_queue(queue: dict) -> None:
    _save_queue(queue, JOB_QUEUE_PATH)


def print_summary() -> None:
    queue = load_queue()
    jobs = queue["jobs"]

    counts: dict[str, int] = {}
    for job in jobs:
        s = job.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    print("\n=== Job Search Tracker ===")
    for status, count in sorted(counts.items()):
        icon = STATUS_EMOJI.get(status, "?")
        print(f"  {icon}  {status:<15} {count}")
    print(f"\n  Total: {len(jobs)} jobs tracked")


# Jobs the user can act on right now: either a queued candidate that hasn't
# been applied to yet, or an application started but not finished. Every
# post-application status (applied and later) has moved out of this view
# and belongs to history_groups() instead - once you've applied, there's no
# further action to take here.
#
# "discovered" and "filtered_out" are deliberately absent from both
# ACTIONABLE_GROUPS and HISTORY_GROUPS: a discovered job needs scoring
# before it's actionable, and a filtered-out job is a terminal non-match,
# not something requiring - or ever having received - a decision. Both
# still show up in print_summary()'s full status counts.
ACTIONABLE_GROUPS = {
    "In progress": ("in_progress",),
    "Ready to apply": ("shortlisted", "board_approved"),
}

# Every status reachable only after an application was actually submitted.
# Order matches the grouping requested for `history`: applied/waiting,
# interviewing, rejected, closed, offers.
HISTORY_GROUPS = {
    "Applied / Waiting": ("applied",),
    "Interviewing": ("interviewing", "questionnaire_submitted"),
    "Rejected": ("rejected",),
    "Closed": ("closed",),
    "Offers": ("offer",),
}

# Per-status timestamp field used when displaying a history entry - each
# points at the specific field update_status() sets for that transition
# (see STATUS_TIMESTAMP_FIELD's use there), not a generic "last_updated".
STATUS_TIMESTAMP_FIELD = {
    "applied": "applied_date",
    "interviewing": "interviewing_at",
    "questionnaire_submitted": "questionnaire_submitted_at",
    "offer": "offer_at",
    "rejected": "rejected_at",
    "closed": "closed_or_expired_at",
}


def _group_jobs(jobs: list[dict], groups: dict[str, tuple]) -> dict[str, list[dict]]:
    """Partition jobs into named groups by status.

    Each status in `groups` must belong to exactly one group's status tuple
    - ACTIONABLE_GROUPS and HISTORY_GROUPS both satisfy this by construction
    - so a job lands in exactly one bucket. Jobs whose status matches none
    of the tuples (e.g. "discovered", "filtered_out") are simply omitted,
    not silently misfiled into an unrelated group.
    """
    result: dict[str, list[dict]] = {label: [] for label in groups}
    for job in jobs:
        status = job.get("status")
        for label, statuses in groups.items():
            if status in statuses:
                result[label].append(job)
                break
    return result


def actionable_groups(jobs: list[dict]) -> dict[str, list[dict]]:
    return _group_jobs(jobs, ACTIONABLE_GROUPS)


def history_groups(jobs: list[dict]) -> dict[str, list[dict]]:
    return _group_jobs(jobs, HISTORY_GROUPS)


def _print_job_line(job: dict, timestamp: str | None = None) -> None:
    score = job.get("fit_score", "?")
    suffix = f"  ({timestamp})" if timestamp else ""
    print(f"    [{job.get('status')}] {job.get('title')} @ {job.get('company')}  (score: {score}){suffix}")


def print_pipeline() -> None:
    queue = load_queue()
    groups = actionable_groups(queue["jobs"])

    print("\n=== Actionable Pipeline ===")
    total = 0
    for label, jobs in groups.items():
        print(f"\n  {label} ({len(jobs)})")
        for job in sorted(jobs, key=lambda j: -(j.get("fit_score") or 0)):
            _print_job_line(job)
        total += len(jobs)
    print(f"\n  Total actionable: {total}")


def print_history() -> None:
    queue = load_queue()
    groups = history_groups(queue["jobs"])

    print("\n=== Application History ===")
    total = 0
    for label, jobs in groups.items():
        print(f"\n  {label} ({len(jobs)})")
        for job in jobs:
            field = STATUS_TIMESTAMP_FIELD.get(job.get("status"))
            timestamp = job.get(field) if field else None
            _print_job_line(job, timestamp)
        total += len(jobs)
    print(f"\n  Total: {total}")


def update_status(company: str, title: str, new_status: str, notes: str = "") -> None:
    if new_status not in VALID_STATUSES:
        print(f"[tracker] Invalid status '{new_status}'. Choose from: {VALID_STATUSES}")
        return

    queue = load_queue()
    for job in queue["jobs"]:
        if job.get("company", "").lower() == company.lower() and job.get("title", "").lower() == title.lower():
            job["status"] = new_status
            job["last_updated"] = str(date.today())
            now = datetime.now(timezone.utc).isoformat()
            today = str(date.today())

            # Every post-shortlist status backfills the "applied" markers
            # (via setdefault, so an existing real timestamp is never
            # overwritten) so lifecycle_metrics can measure shortlisted ->
            # applied duration even for a job recorded straight into a
            # later status - e.g. from a confirmation email received well
            # after the fact - without ever passing through an explicit
            # "applied" update first.
            if new_status in {"applied", "interviewing", "questionnaire_submitted", "offer", "rejected"}:
                job.setdefault("applied_date", today)
                job.setdefault("application_submitted_at", now)

            # Each transition also receives its own precise, status-specific
            # timestamp, distinct from the "applied" backfill above.
            if new_status == "interviewing":
                job.setdefault("interviewing_at", now)
            elif new_status == "questionnaire_submitted":
                job.setdefault("questionnaire_submitted_at", now)
            elif new_status == "offer":
                job.setdefault("offer_at", now)
            elif new_status == "rejected":
                job.setdefault("rejected_at", now)
            elif new_status == "closed":
                job.setdefault("closed_or_expired_at", now)

            if notes:
                job.setdefault("notes", []).append(f"{today}: {notes}")
            save_queue(queue)
            print(f"[tracker] Updated {title} @ {company} → {new_status}")
            return

    print(f"[tracker] Job not found: {title} @ {company}")


if __name__ == "__main__":
    print_summary()
    print_pipeline()
    print_history()
