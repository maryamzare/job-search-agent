"""
Job lifecycle timing metrics.

Answers a question the pipeline had no way to answer before: once a job
is shortlisted, how long until it's actually applied to - and which jobs
never make it there at all? Prior session notes recorded this happening
in practice (high-scoring jobs lost to "shortlist-sitting"), but there
was nothing measuring it, only the anecdote.

Three lifecycle timestamps feed this (set at the point each transition
happens - see module2_scoring.score_all_discovered, module5_apply.
apply_to_shortlisted, and module6_tracker.update_status):

  - shortlisted_at: when a job's fit score first cleared MIN_FIT_SCORE
  - application_submitted_at: when the human confirmed they applied
  - closed_or_expired_at: when a job's status was set to "closed"

All three are only ever set going forward from when this instrumentation
shipped - there is no way to recover when a job already sitting in
job_queue.json was actually shortlisted or applied to, so backfilling
would mean fabricating data. compute_metrics() is built to degrade
gracefully instead: duration metrics (average/median hours) only use
jobs that have both of the relevant timestamps, while status-only
metrics (never applied, closed before application) work from status
alone and are meaningful immediately, even before any job has both
timestamps recorded.
"""
import statistics
from datetime import datetime, timezone

# Jobs sitting somewhere between "passed the fit bar" and "applied" -
# these are the ones that can still be lost to shortlist-sitting.
PENDING_STATUSES = {"shortlisted", "board_approved", "in_progress"}

# Statuses only reachable after an application was actually submitted -
# used to avoid miscounting a job as "never applied" just because it
# predates the application_submitted_at field.
POST_APPLICATION_STATUSES = {"applied", "interviewing", "questionnaire_submitted", "offer", "rejected"}

DEFAULT_HIGH_SCORE_THRESHOLD = 80
DEFAULT_DELAY_THRESHOLD_HOURS = 48


def _parse_iso(value) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning None (never raising) for
    missing or malformed values - job_queue.json holds hand-edited and
    legacy data, so this needs to degrade gracefully, not crash a report."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def hours_between(start_iso, end_iso) -> float | None:
    """Hours from start_iso to end_iso, or None if either is missing or
    unparseable."""
    start = _parse_iso(start_iso)
    end = _parse_iso(end_iso)
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 3600.0


def compute_metrics(
    jobs: list,
    high_score_threshold: int = DEFAULT_HIGH_SCORE_THRESHOLD,
    delay_threshold_hours: float = DEFAULT_DELAY_THRESHOLD_HOURS,
    now: datetime = None,
) -> dict:
    """Compute shortlist-to-apply timing metrics over a list of job dicts.

    now: injectable for deterministic tests; defaults to the real current
    time, used only to measure how long a still-pending job has been
    waiting.
    """
    now = now or datetime.now(timezone.utc)

    completed_durations = []
    never_applied_jobs = []
    closed_before_application_jobs = []
    high_score_delayed_jobs = []

    for job in jobs:
        status = job.get("status")
        shortlisted_at = job.get("shortlisted_at")
        applied_at = job.get("application_submitted_at")
        fit_score = job.get("fit_score")
        company = job.get("company")
        title = job.get("title")

        hours = hours_between(shortlisted_at, applied_at)
        if hours is not None:
            completed_durations.append(hours)
            if fit_score is not None and fit_score >= high_score_threshold and hours > delay_threshold_hours:
                high_score_delayed_jobs.append({
                    "company": company,
                    "title": title,
                    "fit_score": fit_score,
                    "hours": round(hours, 1),
                    "reason": "applied_late",
                })

        if status in PENDING_STATUSES:
            never_applied_jobs.append({
                "company": company,
                "title": title,
                "status": status,
                "fit_score": fit_score,
            })

            start = _parse_iso(shortlisted_at)
            if start is not None and fit_score is not None and fit_score >= high_score_threshold:
                elapsed_hours = (now - start).total_seconds() / 3600.0
                if elapsed_hours > delay_threshold_hours:
                    high_score_delayed_jobs.append({
                        "company": company,
                        "title": title,
                        "fit_score": fit_score,
                        "hours": round(elapsed_hours, 1),
                        "reason": "still_pending",
                    })

        if status == "closed" and applied_at is None:
            closed_before_application_jobs.append({
                "company": company,
                "title": title,
                "fit_score": fit_score,
            })

    return {
        "high_score_threshold": high_score_threshold,
        "delay_threshold_hours": delay_threshold_hours,
        "jobs_with_both_timestamps": len(completed_durations),
        "average_shortlist_to_apply_hours": (
            round(statistics.mean(completed_durations), 1) if completed_durations else None
        ),
        "median_shortlist_to_apply_hours": (
            round(statistics.median(completed_durations), 1) if completed_durations else None
        ),
        "never_applied_count": len(never_applied_jobs),
        "never_applied_jobs": sorted(
            never_applied_jobs, key=lambda j: (j["fit_score"] is None, -(j["fit_score"] or 0))
        ),
        "closed_before_application_count": len(closed_before_application_jobs),
        "closed_before_application_jobs": sorted(
            closed_before_application_jobs, key=lambda j: (j["fit_score"] is None, -(j["fit_score"] or 0))
        ),
        "high_score_delayed_count": len(high_score_delayed_jobs),
        "high_score_delayed_jobs": sorted(
            high_score_delayed_jobs, key=lambda j: -(j["fit_score"] or 0)
        ),
    }


def generate_report(metrics: dict) -> str:
    """Render compute_metrics()'s output as a markdown report."""
    lines = [
        "# Pipeline Timing Report",
        "",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        f"**High-score threshold:** {metrics['high_score_threshold']}  |  "
        f"**Delay threshold:** {metrics['delay_threshold_hours']}h",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Jobs with complete shortlist→apply timing data | {metrics['jobs_with_both_timestamps']} |",
        f"| Average shortlist-to-apply time | {_fmt_hours(metrics['average_shortlist_to_apply_hours'])} |",
        f"| Median shortlist-to-apply time | {_fmt_hours(metrics['median_shortlist_to_apply_hours'])} |",
        f"| Shortlisted jobs never applied (currently pending) | {metrics['never_applied_count']} |",
        f"| Jobs closed before application | {metrics['closed_before_application_count']} |",
        f"| High-score jobs delayed (>{metrics['delay_threshold_hours']}h) | {metrics['high_score_delayed_count']} |",
        "",
        "## Actionable Insights",
        "",
    ]

    if metrics["jobs_with_both_timestamps"] == 0:
        lines.append(
            "- No jobs yet have both `shortlisted_at` and `application_submitted_at` recorded. "
            "This instrumentation was just added — timing metrics will populate as jobs move "
            "through discover → score → apply going forward. Status-based metrics below "
            "(never-applied, closed-before-application) already reflect the current queue."
        )

    if metrics["high_score_delayed_count"] > 0:
        lines.append(
            f"- **{metrics['high_score_delayed_count']} high-score job(s) "
            f"(fit_score ≥ {metrics['high_score_threshold']}) are delayed past "
            f"{metrics['delay_threshold_hours']}h — apply to these first:**"
        )
        for j in metrics["high_score_delayed_jobs"]:
            status_note = "still pending" if j["reason"] == "still_pending" else "took this long to apply"
            lines.append(f"  - {j['company']} — {j['title']} (score {j['fit_score']}, {j['hours']}h, {status_note})")

    if metrics["closed_before_application_count"] > 0:
        lines.append(
            f"- **{metrics['closed_before_application_count']} job(s) closed before you applied** "
            f"— tightening the shortlist-to-apply loop would recover these:"
        )
        for j in metrics["closed_before_application_jobs"][:10]:
            lines.append(f"  - {j['company']} — {j['title']} (score {j['fit_score']})")

    if metrics["never_applied_count"] > 0:
        lines.append(
            f"- **{metrics['never_applied_count']} shortlisted job(s) are still pending, never applied:**"
        )
        for j in metrics["never_applied_jobs"][:10]:
            lines.append(f"  - {j['company']} — {j['title']} (score {j['fit_score']}, status: {j['status']})")

    if (
        metrics["jobs_with_both_timestamps"] > 0
        and metrics["never_applied_count"] == 0
        and metrics["closed_before_application_count"] == 0
        and metrics["high_score_delayed_count"] == 0
    ):
        lines.append("- No delays or losses detected in the current queue — the shortlist-to-apply loop is healthy.")

    return "\n".join(lines) + "\n"


def _fmt_hours(value) -> str:
    return f"{value}h" if value is not None else "no data yet"
