"""
main.py — Job Search Agent Orchestrator
Usage:
  python main.py add <path>   — manually add a job from a JSON file to the queue
  python main.py discover     — find new jobs and add to queue
  python main.py score        — score all discovered jobs
  python main.py resume       — tailor resumes for shortlisted jobs
  python main.py coverletter  — write cover letters for shortlisted jobs
  python main.py apply        — walk through shortlisted jobs to apply
  python main.py status       — show tracker summary
  python main.py pipeline     — show actionable jobs (in progress / ready to apply)
  python main.py history      — show applied/waiting, interviewing, rejected, closed, offers
  python main.py update <company> <title> <status>
                               — record a status transition (applied, interviewing,
                                 questionnaire_submitted, offer, rejected, closed, ...)
  python main.py run          — run lean pipeline: discover → score → resume
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
    from config import JOB_QUEUE_PATH
    from modules.util import load_queue
    queue = load_queue(JOB_QUEUE_PATH)
    for job in queue["jobs"]:
        if job.get("status") in ("shortlisted", "board_approved"):
            resume.tailor_and_save(job)


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
    print("=== Running lean interview-throughput pipeline ===")
    cmd_discover()
    cmd_score()
    cmd_resume()
    print("=== Materials ready. Run 'python main.py apply'; generate cover letters only when required. ===")


COMMANDS = {
    "add": cmd_add,
    "discover": cmd_discover,
    "score": cmd_score,
    "resume": cmd_resume,
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
