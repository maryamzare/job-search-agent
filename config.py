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
LLM_USAGE_LOG_PATH = "data/llm_usage_log.jsonl"

# --- Candidate Profile ---
# Single source of truth for candidate facts sent to Claude. Previously
# duplicated (with drifting wording) as CANDIDATE_PROFILE in
# module2_scoring.py and CANDIDATE_CONTEXT in module4_coverletter.py.
CANDIDATE_PROFILE = """Name: Marmar
Title: Senior TPM
Experience: 8+ years
Education: B.S. CS/SWE (UW), M.S. AI Management (Georgetown, 2027)
Certifications: PMP, SAFe, CSM
Target roles: Senior TPM, AI Product Manager, Engineering Manager
Industries: AI/ML, healthtech, data platforms, enterprise SaaS
Location: Seattle, WA — hybrid or remote preferred"""
