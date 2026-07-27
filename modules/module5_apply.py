"""
Module 5 — Application Submission
Opens each shortlisted job URL, waits for manual form submission,
then confirms with the user before updating the tracker.
"""

import webbrowser
from datetime import date
from pathlib import Path
from config import JOB_QUEUE_PATH, RESUME_OUTPUT_DIR, COVERLETTER_OUTPUT_DIR
from modules.util import slugify, load_queue, save_queue


def print_application_materials(job: dict) -> None:
    slug = slugify(f"{job.get('company', 'company')}_{job.get('title', 'role')}")
    resume_path = Path(RESUME_OUTPUT_DIR) / f"{slug}.txt"
    cl_path = Path(COVERLETTER_OUTPUT_DIR) / f"{slug}.txt"

    print("\n" + "=" * 60)
    print(f"  {job.get('title')}")
    print(f"  {job.get('company')}  |  Score: {job.get('fit_score')}/100")
    print("=" * 60)
    print(f"\n  RESUME:       {resume_path}")
    print(f"  COVER LETTER: {cl_path}")
    print(f"  APPLY URL:    {job.get('apply_url') or job.get('url', 'unknown')}")
    print()

    if not resume_path.exists():
        print(f"  WARNING: Resume file not found — run 'python3 main.py resume' first")
    if not cl_path.exists():
        print(f"  WARNING: Cover letter not found — run 'python3 main.py coverletter' first")


def apply_to_shortlisted() -> None:
    queue = load_queue(JOB_QUEUE_PATH)

    shortlisted = [j for j in queue["jobs"] if j.get("status") in ("shortlisted", "board_approved")]
    if not shortlisted:
        print("[apply] No shortlisted/board-approved jobs. Run 'python3 main.py pipeline' to check your queue.")
        return

    print(f"\n[apply] Starting application flow for {len(shortlisted)} job(s).")
    print("[apply] For each role: review your materials, complete the form, then confirm here.\n")

    submitted = 0
    skipped = 0

    for i, job in enumerate(shortlisted, 1):
        print(f"\n--- Job {i} of {len(shortlisted)} ---")
        print_application_materials(job)

        url = job.get("apply_url") or job.get("url")
        if url:
            input("  Press ENTER to open the application in your browser...")
            webbrowser.open(url)
            print(f"  Opened: {url}")
        else:
            print("  No URL on file — navigate to the portal manually.")

        print("\n  Complete and submit the application form in your browser.")
        print("  Come back here when you're done.\n")

        while True:
            answer = input("  Did you submit this application? [y/n]: ").strip().lower()
            if answer in ("y", "n"):
                break
            print("  Please enter y or n.")

        if answer == "y":
            job["status"] = "applied"
            job["applied_date"] = str(date.today())
            print(f"  Marked as APPLIED ✓")
            submitted += 1
        else:
            job["status"] = "in_progress"
            print(f"  Marked as IN_PROGRESS — come back to finish this one.")
            skipped += 1

    save_queue(queue, JOB_QUEUE_PATH)

    print(f"\n[apply] Done. {submitted} submitted, {skipped} left in progress.")
    print("[apply] Run 'python3 main.py status' to see your full pipeline.\n")


if __name__ == "__main__":
    apply_to_shortlisted()
