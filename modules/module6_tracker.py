"""
Module 6 — Application Tracker
Displays the current state of job_queue.json and allows status updates.
Statuses: discovered → shortlisted | filtered_out → applied → interviewing → offer | rejected
"""

import json
from datetime import date
from config import JOB_QUEUE_PATH

VALID_STATUSES = [
    "discovered",
    "shortlisted",
    "filtered_out",
    "applied",
    "interviewing",
    "offer",
    "rejected",
]

STATUS_EMOJI = {
    "discovered": "🔍",
    "shortlisted": "⭐",
    "filtered_out": "✗",
    "applied": "📤",
    "interviewing": "🗣",
    "offer": "🎉",
    "rejected": "✗",
}


def load_queue() -> dict:
    with open(JOB_QUEUE_PATH) as f:
        return json.load(f)


def save_queue(queue: dict) -> None:
    with open(JOB_QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2)


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


def print_pipeline(exclude_statuses=None) -> None:
    exclude = set(exclude_statuses or ["filtered_out"])
    queue = load_queue()

    print("\n=== Active Pipeline ===")
    for job in queue["jobs"]:
        if job.get("status") in exclude:
            continue
        score = job.get("fit_score", "?")
        print(f"  [{job.get('status')}] {job.get('title')} @ {job.get('company')}  (score: {score})")


def update_status(company: str, title: str, new_status: str, notes: str = "") -> None:
    if new_status not in VALID_STATUSES:
        print(f"[tracker] Invalid status '{new_status}'. Choose from: {VALID_STATUSES}")
        return

    queue = load_queue()
    for job in queue["jobs"]:
        if job.get("company", "").lower() == company.lower() and job.get("title", "").lower() == title.lower():
            job["status"] = new_status
            job["last_updated"] = str(date.today())
            if notes:
                job.setdefault("notes", []).append(f"{date.today()}: {notes}")
            save_queue(queue)
            print(f"[tracker] Updated {title} @ {company} → {new_status}")
            return

    print(f"[tracker] Job not found: {title} @ {company}")


if __name__ == "__main__":
    print_summary()
    print_pipeline()
