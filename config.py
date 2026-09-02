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
LINKEDIN_LOOKBACK_HOURS = 24  # only fetch LinkedIn postings from the last N hours
MIN_FIT_SCORE = 70  # 0-100; jobs below this are filtered out

# --- File Paths ---
MASTER_RESUME_PATH = "data/master_resume.txt"
MASTER_RESUME_PM_PATH = "data/master_resume_pm.txt"
MASTER_RESUME_PM_PIVOT_PATH = "data/master_resume_pm_pivot.txt"
MASTER_RESUME_SOLUTIONS_PATH = "data/master_resume_solutions.txt"
JOB_QUEUE_PATH = "data/job_queue.json"
RESUME_OUTPUT_DIR = "outputs/tailored_resumes"
COVERLETTER_OUTPUT_DIR = "outputs/cover_letters"
LLM_USAGE_LOG_PATH = "data/llm_usage_log.jsonl"
PIPELINE_STAGE_LOG_PATH = "data/pipeline_stage_log.jsonl"

# --- Candidate Profile ---
# Single source of truth for candidate facts sent to Claude. Previously
# duplicated (with drifting wording) as CANDIDATE_PROFILE in
# module2_scoring.py and CANDIDATE_CONTEXT in module4_coverletter.py.
CANDIDATE_PROFILE = """Name: Marmar
Title: Senior TPM
Experience: 8+ years
Education: B.S. CS/SWE (UW, 2017), M.S. AI Management (Georgetown, 2027), B.S. Architecture (Azad University/IAU, 2008-2011)
Certifications: PMP, SAFe, CSM, AI Foundations (OpenAI Academy), Applied AI Foundations (OpenAI Academy)
People management: directly managed a team of 3 junior developers (task planning, code review, technical growth) at IHME/University of Washington, 2019-2022
Design/UX background: formal architecture education (visual/spatial design); at IHME, interviewed stakeholders to define requirements, then storyboarded and designed the application in Figma for a WHO rehabilitation-needs visualization tool
Target roles: Senior TPM, AI Product Manager, Engineering Manager, Product Manager (pivot, design-led positioning), Solutions Engineer / Sales Engineer, AI Implementation Consultant, Developer Advocate/DevRel
Customer-facing/sales background: 8+ years translating customer/business needs into requirements, specs, user stories for engineering and product teams; worked directly with sales engineers at INRIX translating customer/market feedback into product decisions; early-career customer-facing sales experience (interior design)
AI capability statement: Identifies, deploys, governs, measures, and scales economically valuable AI workflows inside an enterprise
Industries: AI/ML, healthtech, data platforms, enterprise SaaS
Location: Seattle, WA — hybrid or remote preferred"""
