import os

# --- API Keys ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Model ---
CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096

# --- Job Search Filters ---
TARGET_ROLES = [
    "Senior Technical Program Manager",
    "Senior TPM",
    "AI Product Manager",
    "Engineering Manager",
]
TARGET_INDUSTRIES = ["AI", "ML", "healthtech", "data platform", "enterprise SaaS"]
TARGET_LOCATIONS = ["Seattle", "Remote", "Hybrid"]
MIN_FIT_SCORE = 70  # 0-100; jobs below this are filtered out

# --- File Paths ---
MASTER_RESUME_PATH = "data/master_resume.txt"
JOB_QUEUE_PATH = "data/job_queue.json"
RESUME_OUTPUT_DIR = "outputs/tailored_resumes"
COVERLETTER_OUTPUT_DIR = "outputs/cover_letters"
