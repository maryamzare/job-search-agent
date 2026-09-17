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
TARGET_LOCATIONS = ["Seattle", "Remote", "United States", "Hybrid"]
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

# --- Single-job resume generation (see modules/module3_resume.py) ---
# The authoritative career profile is the ONLY factual source for the new
# single-job resume path. Historical master resumes above are not fact inputs
# to it (they remain in use by module2_scoring and module4_coverletter).
CAREER_PROFILE_PATH = "data/career_profile_source_of_truth.md"
# User-owned, git-ignored, chmod 600. Only source for the resume's contact
# line. Never sent to model calls; never printed in logs or errors.
CONTACT_BLOCK_PATH = "data/contact_block.txt"
# Blocked-job review reports (targeted questions) — kept separate from finals.
RESUME_REVIEW_DIR = "outputs/resume_reviews"
RESUME_REVIEW_RESOLVED_DIR = "outputs/resume_reviews/resolved"
# Scratch space for the temp .docx before it is validated and promoted.
RESUME_BUILD_TMP_DIR = "outputs/tailored_resumes/.build"
# Where a --replace run moves the previous final before writing the new one.
RESUME_SUPERSEDED_DIR = "outputs/tailored_resumes/superseded"

# --- Scoring: location-fit exception (see module2_scoring.LOCATION_RULE) ---
# Companies large/well-known enough that a non-Seattle U.S. location alone
# should not count against fit, when pay and skills match are also strong.
# Maintained list — add/remove companies here; nothing else needs to change.
BIG_COMPANIES = [
    "Amazon", "Apple", "Google", "Microsoft", "Meta", "Anthropic", "OpenAI",
    "Oracle", "NVIDIA", "Stripe", "Databricks", "Salesforce", "Adobe", "SAP",
    "Qualcomm", "Uber", "Airbnb", "Netflix", "Cisco", "Intel", "IBM", "PayPal",
    "Visa", "Mastercard", "Walmart", "Nordstrom", "Expedia Group", "T-Mobile",
    "Costco", "Snowflake", "Atlassian", "ServiceNow", "Workday", "Palantir",
    "DoorDash", "Coinbase", "CrowdStrike", "Boeing",
]
# User-selected total-comp floor (USD/yr) for a non-Seattle U.S. location.
# Unstated compensation does not establish that this floor is met.
HIGH_PAY_TC_USD = 300_000

# --- Candidate Profile ---
# Single source of truth for candidate facts sent to Claude. Previously
# duplicated (with drifting wording) as CANDIDATE_PROFILE in
# module2_scoring.py and CANDIDATE_CONTEXT in module4_coverletter.py.
CANDIDATE_PROFILE = """Name: Marmar
Title: Senior TPM
Experience: 8+ years
Education: B.S. CS/SWE (UW, 2017), M.S. AI Management (Georgetown, 2027), B.S. Architecture (Azad University/IAU, 2008-2011)
Certifications: PMP, CSM, AI Foundations (OpenAI Academy), Applied AI Foundations (OpenAI Academy), Agents and Workflows (OpenAI Academy)
People management: directly managed a team of 3 junior developers (task planning, code review, technical growth) at IHME/University of Washington, 2019-2022
Design/UX background: formal architecture education (visual/spatial design); at IHME, interviewed stakeholders to define requirements, then storyboarded and designed the application in Figma for a WHO rehabilitation-needs visualization tool
Target roles: Senior TPM, AI Product Manager, Engineering Manager, Product Manager (pivot, design-led positioning), Solutions Engineer / Sales Engineer, AI Implementation Consultant, Developer Advocate/DevRel
Customer-facing/sales background: 8+ years translating customer/business needs into requirements, specs, user stories for engineering and product teams; worked directly with sales engineers at INRIX translating customer/market feedback into product decisions; early-career customer-facing sales experience (interior design)
AI capability statement: Identifies, deploys, governs, measures, and scales economically valuable AI workflows inside an enterprise
Additional skill: Literary Agility
Industries: AI/ML, healthtech, data platforms, enterprise SaaS
Location: Seattle, WA — hybrid or remote preferred"""
