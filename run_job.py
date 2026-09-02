"""Run the lean pipeline for one already-queued job."""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from config import JOB_QUEUE_PATH
from modules import module2_scoring as scoring
from modules import module3_resume as resume
from modules.util import load_queue


if len(sys.argv) != 3:
    raise SystemExit("Usage: python3 run_job.py <company> <title-substring>")

company, title_keyword = sys.argv[1:]
scoring.score_all_discovered()
queue = load_queue(JOB_QUEUE_PATH)
job = next(
    (j for j in queue["jobs"] if j.get("company") == company and title_keyword in j.get("title", "")),
    None,
)
if job is None:
    raise SystemExit(f"Job not found: {title_keyword} @ {company}")
if job.get("status") not in {"shortlisted", "board_approved", "in_progress"}:
    raise SystemExit(f"Job is not eligible to apply: status={job.get('status')} score={job.get('fit_score')}")

path = resume.tailor_and_save(job)
print(f"Resume ready: {path}")
print("Next: python3 main.py apply")
