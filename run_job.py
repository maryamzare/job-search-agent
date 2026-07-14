"""One-off pipeline runner for a single manually-added job."""
import sys, os, json, asyncio, re

os.chdir("/Users/marmar/job-search-agent")
from dotenv import load_dotenv
load_dotenv("/Users/marmar/job-search-agent/.env")

sys.path.insert(0, "/Users/marmar/job-search-agent")
sys.path.insert(0, "/Users/marmar/job-search-agent/modules")

import module2_scoring as scoring
import module2b_board_review as board
import module3_resume as resume
import module3b_resume_board as resume_board
import module4_coverletter as coverletter
from config import JOB_QUEUE_PATH, MASTER_RESUME_PATH

COMPANY = sys.argv[1]
TITLE_KEYWORD = sys.argv[2]
FORCE = "--force" in sys.argv  # override scorer and board

def slugify(t):
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")

# 1. Score
print("=== SCORING ===")
scoring.score_all_discovered()

with open(JOB_QUEUE_PATH) as f:
    queue = json.load(f)
job = next(j for j in queue["jobs"] if j["company"] == COMPANY and TITLE_KEYWORD in j["title"])
print(f"Score: {job.get('fit_score')} | Status: {job.get('status')}")

if job.get("status") == "filtered_out":
    if FORCE:
        print("Score below threshold — overriding per user request")
        job["status"] = "shortlisted"
        with open(JOB_QUEUE_PATH, "w") as f:
            json.dump(queue, f, indent=2)
    else:
        print("Filtered out. Re-run with --force to override.")
        sys.exit(0)

# 2. Board review
print("\n=== BOARD REVIEW ===")
with open(MASTER_RESUME_PATH) as f:
    master = f.read()
result = asyncio.run(board.run_advisory_board(job, master))
job["board_reviews"] = result["reviews"]
job["board_decision"] = result["board_decision"]
decision = result["board_decision"]
print(f"action={decision.get('action')}  score={decision.get('composite_score')}")
print(f"strength: {decision.get('top_strength','')[:100]}")
print(f"concern:  {decision.get('top_concern','')[:100]}")

if decision.get("action") != "apply":
    if FORCE:
        print("Board says defer — overriding per user request, proceeding")
    else:
        print("Board says defer/skip. Re-run with --force to override.")
        with open(JOB_QUEUE_PATH, "w") as f:
            json.dump(queue, f, indent=2)
        sys.exit(0)

job["status"] = "board_approved"

# 3. Resume tailoring
print("\n=== RESUME TAILORING ===")
slug = slugify(f"{job['company']}_{job['title']}")
v1path = f"outputs/tailored_resumes/{slug}.txt"
if os.path.exists(v1path):
    os.remove(v1path)
resume.tailor_and_save(job)

# 4. Resume board
print("\n=== RESUME BOARD ===")
with open(v1path) as f:
    draft = f.read()
rb_result = asyncio.run(resume_board.review_resume(draft, job))
job["resume_board"] = rb_result["raw_reviews"]
job["resume_scorecard"] = rb_result["scorecard"]
v2path = f"outputs/tailored_resumes/{slug}_v2.txt"
with open(v2path, "w") as f:
    f.write(rb_result["final_resume"])
job["resume_v2_path"] = v2path
sc = rb_result["scorecard"]
print(f"composite={sc.get('composite_score')}  ats={sc.get('ats_score')}  impact={sc.get('impact_score')}  ready={sc.get('ready_to_submit')}")
if sc.get("blocker"):
    print(f"blocker: {sc['blocker'][:200]}")

# 5. Cover letter
print("\n=== COVER LETTER ===")
coverletter.generate_and_save(job)

# Save queue
with open(JOB_QUEUE_PATH, "w") as f:
    json.dump(queue, f, indent=2)

print("\n=== DONE ===")
