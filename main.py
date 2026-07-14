"""
main.py — Job Search Agent Orchestrator
Usage:
  python main.py add <path>   — manually add a job from a JSON file to the queue
  python main.py discover     — find new jobs and add to queue
  python main.py score        — score all discovered jobs
  python main.py resume       — tailor resumes for shortlisted jobs
  python main.py coverletter  — write cover letters for shortlisted jobs
  python main.py apply        — walk through shortlisted jobs to apply
  python main.py board        — run advisory board review on all shortlisted jobs
  python main.py resumeboard  — run resume advisory board + rewrite on shortlisted/board_approved jobs
  python main.py status       — show tracker summary
  python main.py pipeline     — show active pipeline (excludes filtered)
  python main.py run          — run full pipeline: discover → score → board → resume → coverletter
"""

import sys
from dotenv import load_dotenv

load_dotenv()

import modules.module1_discovery as discovery
import modules.module2_scoring as scoring
import modules.module2b_board_review as board
import modules.module3_resume as resume
import modules.module3b_resume_board as resume_board
import modules.module4_coverletter as coverletter
import modules.module5_apply as apply_module
import modules.module6_tracker as tracker


def cmd_add():
    import json
    from config import JOB_QUEUE_PATH
    if len(sys.argv) < 3:
        print("Usage: python main.py add <path-to-job.json>")
        sys.exit(1)
    path = sys.argv[2]
    with open(path) as f:
        job = json.load(f)
    job["status"] = "discovered"
    with open(JOB_QUEUE_PATH) as f:
        queue = json.load(f)
    existing_urls = {j.get("url") for j in queue["jobs"]}
    if job.get("url") in existing_urls:
        print(f"[add] Already in queue: {job.get('title')} @ {job.get('company')}")
        return
    queue["jobs"].append(job)
    with open(JOB_QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2)
    print(f"[add] Added: {job.get('title')} @ {job.get('company')}")


def cmd_discover():
    jobs = discovery.discover_jobs()
    discovery.add_new_jobs_to_queue(jobs)


def cmd_score():
    scoring.score_all_discovered()


def cmd_resume():
    import json
    from config import JOB_QUEUE_PATH
    with open(JOB_QUEUE_PATH) as f:
        queue = json.load(f)
    for job in queue["jobs"]:
        if job.get("status") in ("shortlisted", "board_approved"):
            resume.tailor_and_save(job)


def cmd_coverletter():
    import json
    from config import JOB_QUEUE_PATH
    with open(JOB_QUEUE_PATH) as f:
        queue = json.load(f)
    for job in queue["jobs"]:
        if job.get("status") in ("shortlisted", "board_approved"):
            coverletter.generate_and_save(job)


def cmd_board():
    board.review_shortlisted()


def cmd_resumeboard():
    resume_board.review_resumes()


def cmd_apply():
    apply_module.apply_to_shortlisted()


def cmd_status():
    tracker.print_summary()


def cmd_pipeline():
    tracker.print_pipeline()


def cmd_run():
    print("=== Running full pipeline ===")
    cmd_discover()
    cmd_score()
    cmd_board()
    cmd_resume()
    cmd_coverletter()
    print("=== Pipeline complete. Run 'python main.py apply' to start applying. ===")


COMMANDS = {
    "add": cmd_add,
    "discover": cmd_discover,
    "score": cmd_score,
    "resume": cmd_resume,
    "coverletter": cmd_coverletter,
    "board": cmd_board,
    "resumeboard": cmd_resumeboard,
    "apply": cmd_apply,
    "status": cmd_status,
    "pipeline": cmd_pipeline,
    "run": cmd_run,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)

    COMMANDS[sys.argv[1]]()
