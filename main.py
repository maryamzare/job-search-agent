"""
main.py — Job Search Agent Orchestrator
Usage:
  python main.py add <path>   — manually add a job from a JSON file to the queue
  python main.py discover     — find new jobs and add to queue
  python main.py score        — score all discovered jobs
  python main.py resume-list  — list jobs eligible for a tailored resume (read-only)
  python main.py resume-one <job-id> [--with-summary] [--replace]
                               — generate ONE validated .docx resume for one job
  python main.py resume       — (removed) prints how to use resume-list / resume-one
  python main.py coverletter  — write cover letters for shortlisted jobs
  python main.py apply        — walk through shortlisted jobs to apply
  python main.py status       — show tracker summary
  python main.py pipeline     — show actionable jobs (in progress / ready to apply)
  python main.py history      — show applied/waiting, interviewing, rejected, closed, offers
  python main.py update <company> <title> <status>
                               — record a status transition (applied, interviewing,
                                 questionnaire_submitted, offer, rejected, closed, ...)
  python main.py run          — discover → score, then STOP (resume generation is
                                 a deliberate one-job-at-a-time step; see resume-list)
"""

import sys
from dotenv import load_dotenv

load_dotenv()

import modules.module1_discovery as discovery
import modules.module2_scoring as scoring
import modules.module3_resume as resume
import modules.module4_coverletter as coverletter
import modules.module5_apply as apply_module
import modules.module6_tracker as tracker


def cmd_add():
    import json
    from config import JOB_QUEUE_PATH
    from modules.util import load_queue, save_queue
    if len(sys.argv) < 3:
        print("Usage: python main.py add <path-to-job.json>")
        sys.exit(1)
    path = sys.argv[2]
    with open(path) as f:
        job = json.load(f)
    job["status"] = "discovered"
    queue = load_queue(JOB_QUEUE_PATH)
    existing_urls = {j.get("url") for j in queue["jobs"]}
    if job.get("url") in existing_urls:
        print(f"[add] Already in queue: {job.get('title')} @ {job.get('company')}")
        return
    queue["jobs"].append(job)
    save_queue(queue, JOB_QUEUE_PATH)
    print(f"[add] Added: {job.get('title')} @ {job.get('company')}")


def cmd_discover():
    jobs = discovery.discover_jobs()
    discovery.add_new_jobs_to_queue(jobs)


def cmd_score():
    scoring.score_all_discovered()


def cmd_resume():
    # Batch resume generation was removed: it regenerated every shortlisted job
    # with no per-job selection, no authoritative-profile check, and no
    # validation. Generation now happens one job at a time.
    print("Batch resume generation has been removed.")
    print()
    print("  python3 main.py resume-list                 # eligible job IDs (read-only)")
    print("  python3 main.py resume-one <job-id>         # generate ONE validated resume")
    print("  python3 main.py resume-one <job-id> --with-summary")
    sys.exit(2)


def _parse_resume_one_args(argv):
    """argv is sys.argv[2:]. Returns (job_id, include_summary, replace)."""
    job_id = None
    include_summary = False
    replace = False
    for arg in argv:
        if arg == "--with-summary":
            include_summary = True
        elif arg == "--replace":
            replace = True
        elif arg.startswith("--"):
            print(f"Unknown option: {arg}")
            sys.exit(2)
        elif job_id is None:
            job_id = arg
        else:
            print("Provide exactly one <job-id>.")
            sys.exit(2)
    if not job_id:
        print("Usage: python3 main.py resume-one <job-id> [--with-summary] [--replace]")
        print("Run 'python3 main.py resume-list' to see eligible job IDs.")
        sys.exit(2)
    return job_id, include_summary, replace


def cmd_resume_one():
    job_id, include_summary, replace = _parse_resume_one_args(sys.argv[2:])
    try:
        result = resume.generate_resume_for_job(
            job_id, include_summary=include_summary, replace=replace
        )
    except resume.ResumeError as e:
        print(f"error: {e}")
        sys.exit(e.exit_code)

    if result["status"] == "saved":
        print(f"Saved: {result['path']}")
        print(f"Job ID: {result['job_id']}")
        if result.get("superseded"):
            print(f"Previous version moved to: {result['superseded']}")
        sys.exit(0)
    else:  # blocked
        print("BLOCKED — no resume was saved.")
        print(f"Review report: {result['review_path']}")
        print(f"{len(result['issues'])} issue(s) to resolve, then re-run resume-one.")
        sys.exit(3)


def cmd_resume_list():
    rows = resume.list_eligible_jobs()  # read-only; no API call, no writes
    if not rows:
        print("No jobs are currently eligible (need status shortlisted or board_approved).")
        return
    print(f"{'JOB ID':52}  {'STATUS':14}  {'SCORE':5}  {'LOCATION':22}  COMPANY / TITLE")
    print("-" * 120)
    for r in rows:
        score = str(r["score"]) if r["score"] != "" else "-"
        print(f"{r['job_id']:52}  {r['status']:14}  {score:5}  "
              f"{(r['location'] or '-')[:22]:22}  {r['company']} — {r['title']}")
    print()
    print("Generate one:  python3 main.py resume-one <JOB ID>")


def cmd_coverletter():
    from config import JOB_QUEUE_PATH
    from modules.util import load_queue
    queue = load_queue(JOB_QUEUE_PATH)
    for job in queue["jobs"]:
        if job.get("status") in ("shortlisted", "board_approved"):
            coverletter.generate_and_save(job)


def cmd_apply():
    apply_module.apply_to_shortlisted()


def cmd_status():
    tracker.print_summary()


def cmd_pipeline():
    tracker.print_pipeline()


def cmd_history():
    tracker.print_history()


def cmd_update():
    if len(sys.argv) != 5:
        print("Usage: python main.py update <company> <title> <status>")
        print(f"Valid statuses: {', '.join(tracker.VALID_STATUSES)}")
        sys.exit(1)
    _, _, company, title, new_status = sys.argv
    tracker.update_status(company, title, new_status)


def cmd_run():
    print("=== discover -> score ===")
    cmd_discover()
    cmd_score()
    print()
    print("=== Scored. Resume generation is a separate, one-job-at-a-time step. ===")
    print("  python3 main.py resume-list                 # pick a job")
    print("  python3 main.py resume-one <job-id>         # generate its resume")
    print("  python3 main.py apply                       # once a resume exists")


COMMANDS = {
    "add": cmd_add,
    "discover": cmd_discover,
    "score": cmd_score,
    "resume": cmd_resume,
    "resume-one": cmd_resume_one,
    "resume-list": cmd_resume_list,
    "coverletter": cmd_coverletter,
    "apply": cmd_apply,
    "status": cmd_status,
    "pipeline": cmd_pipeline,
    "history": cmd_history,
    "update": cmd_update,
    "run": cmd_run,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)

    COMMANDS[sys.argv[1]]()
