"""
Module 3 — Single-job resume generation (safe path).

`python3 main.py resume-one <job-id>` is the ONLY way to generate a resume.
There is no batch path. Every generation goes through
`generate_resume_for_job()`, which:

  1. resolves exactly one eligible job by its job_artifact_key
  2. loads data/career_profile_source_of_truth.md — the SOLE factual source
  3. tailors a structured resume to that job's description        [API call 1]
  4. runs the authenticity + formatting pass                       [API call 2]
  5. validates deterministically, then semantically                [API call 3]
  6. on any issue: writes only a review report and stops (no .docx)
  7. on a clean pass: renders one ATS-safe .docx via a temp file,
     verifies it reopens, then atomically moves it into
     outputs/tailored_resumes/<job-id>.docx

Historical master resumes are NOT factual inputs here. The job description is
used for tailoring only, never as evidence of the candidate's experience.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime, timezone

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS,
    CAREER_PROFILE_PATH, CONTACT_BLOCK_PATH,
    RESUME_OUTPUT_DIR, RESUME_REVIEW_DIR, RESUME_REVIEW_RESOLVED_DIR,
    RESUME_BUILD_TMP_DIR, RESUME_SUPERSEDED_DIR,
)
from modules.util import (
    job_artifact_key, load_queue, get_client, tracked_create, track_stage,
    with_retry_sync, parse_llm_json,
)
from config import JOB_QUEUE_PATH

# python-docx lives in .venv (see requirements.txt). Import is guarded so this
# module — and everything that imports it, including the retry/caching unit
# tests — still loads on an interpreter without python-docx; only the actual
# render path then refuses to run.
try:
    from modules import resume_docx
except ImportError:  # pragma: no cover
    resume_docx = None

client = get_client(ANTHROPIC_API_KEY)

ELIGIBLE_STATUSES = ("shortlisted", "board_approved")

# Phrases the resume must not contain unless they appear verbatim in approved
# natural-voice material (the profile's §7 voice notes). Kept lowercase.
BANNED_PHRASES = (
    "results-driven", "results driven", "dynamic professional", "leveraged synergies",
    "proven track record", "synergy", "synergies",
    "detail-oriented", "team player", "go-getter", "hit the ground running",
    "think outside the box", "wheelhouse", "move the needle", "circle back",
    "value add", "value-add", "results-oriented", "self-starter",
)

# Phrases banned with NO exemption, ever -- checked separately from
# BANNED_PHRASES because the exemption above (a phrase is allowed if it
# appears anywhere in the profile text) has a loophole for these: §4.1
# quotes an old resume verbatim as source material ("Spearheaded solution
# design for the GenAI Signals Data Lake"), which made the general
# BANNED_PHRASES check treat "spearheaded" as pre-approved even though that
# quote documents what an old resume said, not permission to reuse the word.
# Checked case-insensitively (comparison is always against the lowercased
# text) with zero exceptions.
ALWAYS_BANNED_PHRASES = (
    "spearheaded",
)

# Em dash (U+2014) is never allowed. En dash (U+2013) is allowed ONLY inside a
# numeric/date range (e.g. "2019–2022", "30–89%", "Jun 2019 – Present"); used as
# sentence punctuation it is flagged. The renderer itself emits plain hyphens.
EM_DASH = "—"
EN_DASH = "–"
_EN_DASH_RANGE_RE = re.compile(
    r"\d\s*–\s*\d"                       # 30–89, 54.6–99
    r"|\d{4}\s*–\s*(?:present|\d{4}|[A-Z][a-z]{2,8}\s+\d{4})"  # 2019–2022 / 2019–Present
    r"|[A-Z][a-z]{2,8}\s+\d{4}\s*–\s*(?:present|[A-Z][a-z]{2,8}\s+\d{4})",  # Jun 2022–Nov 2024
    re.IGNORECASE,
)

# Certifications the profile records as NOT held (career_profile §3 / §8).
FORBIDDEN_CERT_TOKENS = ("safe", "spc", "safe program consultant", "scaled agile", "google ux")

# DO-NOT-COMBINE pairs — career_profile_source_of_truth.md §5.
# Each entry: (name, predicate_a, predicate_b, exception_company_or_None).
# A single bullet triggering both predicates is flagged, unless the bullet's
# role is the documented exception company.
def _has(bullet, *needles):
    b = bullet.lower()
    return all(n in b for n in needles)

DO_NOT_COMBINE = [
    # M2 (UK Signals system performance 54.6% -> 99%) vs M3 (99% accuracy)
    ("M2xM3", lambda b: "54.6" in b, lambda b: "accuracy" in b and "99" in b, None),
    # M9 (+40% completion speed) vs M10 (team of 6 engineers).
    # Documented exception: the S5 Solutions bullet may state both together.
    ("M9xM10",
     lambda b: "40%" in b,
     lambda b: ("six engineer" in b or "6 engineer" in b or "team of 6" in b or "team of six" in b),
     "s5 solutions"),
    # M11 (50+ mentored/guided — influence) vs M12/M13 (3 / 8 direct reports)
    ("M11xM12_13",
     lambda b: "50+" in b or "over 50" in b,
     lambda b: "direct report" in b or "managed a team of 3" in b or "managed 8" in b
               or "8 direct" in b or "3 direct" in b,
     None),
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ResumeError(Exception):
    exit_code = 1


class JobNotFound(ResumeError):
    exit_code = 2


class JobAmbiguous(ResumeError):
    exit_code = 2


class JobIneligible(ResumeError):
    exit_code = 2


class FinalExists(ResumeError):
    exit_code = 2


class MissingInput(ResumeError):
    exit_code = 1


class RenderFailure(ResumeError):
    exit_code = 1


# --------------------------------------------------------------------------- #
# Job resolution (no API call)
# --------------------------------------------------------------------------- #
def list_eligible_jobs() -> list[dict]:
    """Read-only. Return eligible jobs with a computed 'job_id' and 'score'."""
    queue = load_queue(JOB_QUEUE_PATH)
    rows = []
    for job in queue.get("jobs", []):
        if job.get("status") in ELIGIBLE_STATUSES:
            rows.append({
                "job_id": job_artifact_key(job),
                "status": job.get("status", ""),
                "score": job.get("fit_score", job.get("score", "")),
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "location": job.get("location", ""),
                "_job": job,
            })
    rows.sort(key=lambda r: (r["status"], r["company"].lower(), r["title"].lower()))
    return rows


def resolve_job(job_id: str) -> dict:
    """Exact match against the full job_artifact_key. Never an API call."""
    if not job_id or not job_id.strip():
        raise JobNotFound("no job id given")
    queue = load_queue(JOB_QUEUE_PATH)
    matches = [j for j in queue.get("jobs", []) if job_artifact_key(j) == job_id]
    if not matches:
        raise JobNotFound(
            f"no job with id '{job_id}'. Run: python3 main.py resume-list"
        )
    if len(matches) > 1:
        raise JobAmbiguous(
            f"job id '{job_id}' matches {len(matches)} queue entries; cannot proceed"
        )
    job = matches[0]
    if job.get("status") not in ELIGIBLE_STATUSES:
        raise JobIneligible(
            f"job '{job_id}' has status '{job.get('status')}'; "
            f"only {' / '.join(ELIGIBLE_STATUSES)} are eligible"
        )
    return job


# --------------------------------------------------------------------------- #
# Inputs (no API call). Contact values are never logged and never sent to the
# model — they are injected into the .docx only.
# --------------------------------------------------------------------------- #
def load_career_profile() -> str:
    try:
        with open(CAREER_PROFILE_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise MissingInput(f"authoritative career profile unreadable at {CAREER_PROFILE_PATH}: {e.strerror}")
    if len(text.strip()) < 200:
        raise MissingInput(f"authoritative career profile at {CAREER_PROFILE_PATH} looks empty")
    return text


def load_contact_block() -> dict:
    if not os.path.exists(CONTACT_BLOCK_PATH):
        raise MissingInput(
            f"contact block not found at {CONTACT_BLOCK_PATH}. "
            f"Copy data/contact_block.example.txt to it and fill it in."
        )
    contact = {"name": "", "email": "", "phone": "", "location": "",
               "linkedin": "", "github": "", "website": ""}
    with open(CONTACT_BLOCK_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            if key in contact:
                contact[key] = val.strip()
    missing = [k for k in ("name", "location") if not contact[k]]
    if missing:
        raise MissingInput(
            f"contact block at {CONTACT_BLOCK_PATH} is missing required field(s): {', '.join(missing)}"
        )
    return contact


# --------------------------------------------------------------------------- #
# Step 3 — tailor.  Returns raw model text (JSON when career_profile is real).
# Signature keeps a default so the retry/caching unit tests can still call
# tailor_resume(job) with a mocked transport.
# --------------------------------------------------------------------------- #
_SCHEMA_HINT = """Return ONLY a JSON object with this shape:
{
  "summary": string or null,
  "experience": [
    {"title": str, "company": str, "location": str,
     "start": "Mon YYYY" or "YYYY", "end": "Mon YYYY" or "YYYY" or null,
     "current": bool, "bullets": [str, ...]}
  ],
  "skills": [str, ...],
  "education": [{"credential": str, "institution": str, "year": "YYYY" or null}],
  "certifications": [{"name": str, "year": "YYYY" or null}]
}
Do not include a contact section — it is added separately."""

TAILOR_SYSTEM_PROMPT = """You write one candidate's resume, tailored to one job.

Absolute rules:
- The authoritative career profile is the ONLY source of facts. Use nothing else.
- Never invent or infer achievements, responsibilities, skills, metrics, dates,
  titles, certifications, or qualifications.
- The job description tells you what to emphasize and which true experience to
  lead with. It is never evidence that the candidate has done something.
- Never turn a job requirement into a candidate claim.
- If the profile lacks something the job wants, leave it out. Do not paper over gaps.
- Use only metrics the profile states, exactly as stated. Never merge two metrics
  into one claim. Honor the profile's DO-NOT-COMBINE list, including but not
  limited to:
    * M2 (UK Signals system performance, 54.6% -> 99%) and M3 (UK signal
      analytics accuracy, 99%) are DIFFERENT metrics on the same project.
      NEVER let both appear in the same bullet, the same sentence, or two
      clauses of a compound bullet joined by "separately," "in addition,"
      "also," a semicolon, or any other connector meant to signal they are
      distinct. Wording that claims they are distinct does not make sharing a
      bullet acceptable — if both are used at all, each gets its own bullet.
    * M9 (+40% completion speed) and M10 (team of 6 engineers) may share a
      bullet ONLY in the S5 Solutions role — nowhere else.
    * M11 (50+ people mentored/guided — influence, not a reporting line) must
      never be combined with M12 (3 direct reports, IHME) or M13 (8 direct
      reports, INRIX). Influence reach and direct management are different
      claims; never let a bullet imply the larger number is a headcount.
    * The INRIX-Maps figures (6M+ miles, 99.99% reliability, $20M program,
      40% cost reduction, 60% processing/ingestion-time reduction, 210,000
      signalized intersections, 80+ stakeholders) each stand alone. Never
      let two of
      them share a bullet or a sentence, and never let one modify another
      (e.g. "$20M program that cut costs 40%" or "60% faster, reducing
      costs 40%") — even when both figures are true, stating them together
      claims a link between them that the profile does not document.
  Every metric must stay attached only to the project, scope, and meaning the
  profile documents for it — never let a number drift onto a different
  project, role, or claim than the one it was measured on.
- Never attach a descriptive qualifier, scope word, or context to a
  profile-stated fact unless the profile itself states that qualifier.
  "Managed 8 engineers" must not become "managed 8 direct reports across
  engineering functions," "led 8 cross-functional engineers," or similar —
  state the fact exactly as the profile gives it, nothing added, however
  plausible the addition sounds.
- A role's canonical confirmed title (career profile §4, e.g. "Technical
  Program Lead – Maps Platform") must be reproduced exactly, including its
  punctuation. Do not substitute a comma or any other separator for the
  profile's dash.
- Skills must be copied from the profile's confirmed skill list as stated.
  Never blend a profile skill with wording from the job description to make
  it sound more relevant (e.g. the profile's "Distributed Systems" must
  never become "Distributed backend systems" because the job description
  says "backend") — that is a job requirement dressed up as a candidate
  claim, already forbidden above.
- Use each role's dates at exactly the precision the profile documents for
  that role. Do not add a month where the profile gives only a year, and do
  not drop a month where the profile gives one. Different roles legitimately
  have different precision (e.g. some INRIX roles are dated to the month,
  other roles only to the year) — that is not an inconsistency to smooth
  over by inventing or removing precision.
- If the profile marks a credential as not yet completed (e.g. "expected
  YYYY"), the credential string itself must say so explicitly (e.g. "M.S.
  Professional Studies, AI Management (Expected 2027)") — reproduce the
  profile's own credential wording exactly; never abbreviate, reorder, or
  paraphrase it. A bare year in the "year" field is not enough on its own
  and misrepresents an in-progress credential as already awarded.
- Reverse-chronological experience. Every role: title, company, location, dates.
- Prioritize relevance over completeness. Give more bullets, and more detail,
  to the experience most relevant to the target job's domain (e.g. FinTech,
  payments, risk, or financial-services delivery for a FinTech TPM role) and
  fewer bullets to less-relevant roles or workstreams. Dropping a true but
  irrelevant bullet to keep the resume focused is normal tailoring, not the
  same thing as "papering over a gap" (that rule is about not fabricating
  something the job wants that the profile lacks -- it never means every
  true detail must be kept in).
- Plain, specific language in the candidate's own register. Vary how bullets open
  and how they are built; no single repeated "did X to get Y, resulting in Z"
  rhythm; do not force three-item lists.
- No em dashes (—, U+2014) anywhere in the output — not in bullets, the
  summary, headers, or skills. Zero exceptions. If you would reach for an em
  dash, rewrite the sentence with a period, comma, colon, or "and" instead.
  En dashes (–, U+2013) are allowed ONLY inside a literal numeric or date
  range (e.g. "2019–2022", "30–89%") and nowhere else.
- No banned buzzwords (results-driven, dynamic professional,
  leveraged synergies, proven track record, and similar). "Spearheaded" is
  banned with NO exception -- the profile quotes an old resume using that
  word as source material for a fact (§4.1), which is not permission to
  reuse the word itself. Replace it with plain language: "led," "drove,"
  "directed," "built," or similar, chosen to fit what was actually done.
- Include a "summary" only if explicitly told to; otherwise set it to null.
  When included, every sentence in it must be drawn directly from the
  authoritative profile, held to the exact same rules as every bullet above
  -- no invented framing, no job-description language, no buzzwords."""


def _tailor_messages(job: dict, career_profile: str, include_summary: bool):
    stable = f"Authoritative career profile (the ONLY source of facts):\n{career_profile}"
    summ = ("Include a concise 'summary' (2-3 sentences). Every claim in it must "
            "come directly from the authoritative career profile -- nothing "
            "invented, nothing from the job description, no buzzwords."
            if include_summary else "Set 'summary' to null.")
    volatile = f"""

Job to tailor toward (for emphasis and ordering only, NOT as evidence of experience):
Title: {job.get('title')}
Company: {job.get('company')}
Description:
{(job.get('description') or '')[:3500]}

{summ}

{_SCHEMA_HINT}"""
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": volatile},
        ],
    }]


def tailor_resume(job: dict, career_profile: str = "", *, include_summary: bool = False) -> str:
    with track_stage("module3_resume", company=job.get("company"), title=job.get("title")):
        def _call():
            return tracked_create(
                client, "tailor_resume",
                model=CLAUDE_MODEL, max_tokens=RESUME_JSON_MAX_TOKENS,
                system=TAILOR_SYSTEM_PROMPT,
                messages=_tailor_messages(job, career_profile, include_summary),
            )
        response = with_retry_sync(_call)
        return response.content[0].text


# --------------------------------------------------------------------------- #
# Step 4 — authenticity + formatting pass.  Returns a revised resume dict.
# --------------------------------------------------------------------------- #
AUTHENTICITY_SYSTEM_PROMPT = """You revise a resume draft for authenticity and formatting.
You may reword and reorder. You may NOT add, remove, or change any fact, number,
date, title, skill, or certification. If a fact is missing or conflicting, leave
it as-is — do not guess; the validator will catch it.

Make it read like the person, not like AI output:
- vary sentence openings and bullet structure; break up repeated rhythms
- plain words; keep concrete detail about how the work was actually done
- every bullet: a specific action the person took, its context/problem/tools when
  supported, and a profile-stated result only where the profile gives one
- a bullet must be accurate read on its own and must not imply more than the source

Hard rules, zero exceptions:
- No em dashes (—, U+2014) anywhere in the output. If the draft you received
  contains one, remove it by rewriting the sentence — do not just delete the
  dash and leave a run-on or a comma splice. En dashes (–, U+2013) are allowed
  only inside a numeric or date range (e.g. "2019–2022", "30–89%").
- If the draft has M2 (UK Signals system performance, 54.6% -> 99%) and M3
  (UK signal analytics accuracy, 99%) in the same bullet, split them into two
  separate bullets — even if the draft already separates the two claims with
  "separately," a semicolon, or similar wording. That wording does not make it
  acceptable for the two metrics to share a bullet. The same applies to any
  other DO-NOT-COMBINE pair from the career profile (M9/M10 outside the S5
  Solutions bullet; M11 combined with M12 or M13). Never let a metric drift
  onto a project, role, or meaning other than the one the profile attaches it to.
- Strip any descriptive qualifier attached to a fact that the profile does
  not itself state (e.g. "across engineering functions" or "cross-functional"
  tacked onto a bare headcount). State the fact exactly as the profile gives it.
- A role's title must match the profile's canonical title exactly, including
  punctuation — fix a comma (or any other substitution) back to the profile's
  dash if the draft altered it.
- A skill must match the profile's confirmed skill list. Strip any word
  borrowed from the job description that isn't in the profile's own skill
  phrasing (e.g. "backend" added to "Distributed Systems").
- Per-role date precision (month+year vs. year-only) must match what the
  profile documents for that specific role. Do not fabricate a month or drop
  one just to make the formatting look uniform across roles — different
  roles legitimately have different documented precision.
- If a credential is marked in the profile as not yet completed ("expected
  YYYY"), make sure the draft's credential text says so explicitly, not just
  a bare year in the "year" field.
- No banned buzzwords; no keyword stuffing; no forced parallel lists. If the
  draft uses "spearheaded" anywhere, replace it with plain language ("led,"
  "drove," "directed," "built," or similar) -- banned with no exception,
  even though the profile's §4.1 quotes an old resume using that word as
  source material for a fact.

Return ONLY the JSON object in the same schema you received."""


# A full résumé JSON is ~3-4k output tokens on its own; the QC model also tends
# to reason in prose first, so 4096 truncates it mid-object. Give the JSON-
# emitting calls real headroom.
RESUME_JSON_MAX_TOKENS = 8000


def _loose_json_object(text: str):
    """`parse_llm_json` first; then, tolerant of a reasoning preamble the model
    sometimes writes despite instructions, decode the first complete `{...}`
    object in the text. Returns a dict, or None if nothing parses."""
    import json
    parsed = parse_llm_json(text)
    if "parse_error" not in parsed:
        return parsed
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text[i:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        i = text.find("{", i + 1)
    return None


def authenticity_and_format_pass(resume: dict, career_profile: str) -> dict:
    import json
    stable = f"Authoritative career profile (facts must stay within this):\n{career_profile}"
    volatile = "\n\nResume draft to revise (same JSON schema back):\n" + json.dumps(resume, indent=2)
    with track_stage("module3_resume", stage_detail="authenticity"):
        def _call():
            return tracked_create(
                client, "resume_qc:authenticity",
                model=CLAUDE_MODEL, max_tokens=RESUME_JSON_MAX_TOKENS,
                system=AUTHENTICITY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": volatile},
                ]}],
            )
        response = with_retry_sync(_call)
    revised = _loose_json_object(response.content[0].text)
    if revised is None:
        raise RenderFailure("authenticity pass did not return valid JSON")
    return revised


# --------------------------------------------------------------------------- #
# Step 5a — deterministic validation (no API call). Returns a list of issues.
# --------------------------------------------------------------------------- #
_NUM_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?(?:%|m\+?|b\+?|k\+?|\+)?", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
# Metric-shaped: a currency amount, a percentage, a scaled count (12M / 3K+),
# a "N+" count, or a thousands-grouped number. A bare digit stuck to letters
# ("S5", "3M Company") is a name, not a metric, and must not trip the
# role-header check.
_METRIC_SHAPED_RE = re.compile(
    r"\$\s?\d|\d[\d,]*(?:\.\d+)?\s?%|\d[\d,]*(?:\.\d+)?\s?[MBK]\+?\b|\d[\d,]*\+|\d,\d{3}",
    re.IGNORECASE,
)


def _digit_core(tok: str) -> str:
    return re.sub(r"[^\d.]", "", tok)


def _profile_number_cores(profile: str) -> set[str]:
    cores = set()
    for m in _NUM_RE.finditer(profile):
        c = _digit_core(m.group(0))
        if c:
            cores.add(c)
    return cores


def _profile_years(profile: str, section_headers: tuple) -> set[str]:
    """Years that appear anywhere between the given section header and the next
    '## ' header."""
    years = set()
    for hdr in section_headers:
        m = re.search(re.escape(hdr) + r".*?(?=\n## |\Z)", profile, re.S)
        if m:
            years |= {y.group(0) for y in _YEAR_RE.finditer(m.group(0))}
    return years


def _canon_cert(s: str) -> str:
    """Lowercase, drop a trailing year, and collapse harmless formatting
    separators (comma, pipe, slash, colon, parentheses, hyphen, and any
    Unicode dash) to single spaces — so 'Certified Scrum Master, Scrum
    Alliance' and 'Certified Scrum Master — Scrum Alliance' compare equal."""
    s = s.lower()
    s = re.sub(r"\b(19|20)\d{2}\b", " ", s)
    s = re.sub(r"[\s,;:.\|/()\[\]‐-―\-]+", " ", s)
    return s.strip()


def _profile_certs(profile: str):
    """Parse §3. Returns (allowed, forbidden): sets of canonicalized names.
    Each row contributes both 'Name Issuer' and the bare 'Name' (issuer split
    off at a dash/slash), so a resume may display either form."""
    m = re.search(r"## 3\. Certifications.*?(?=\n## 4\.)", profile, re.S)
    span = m.group(0) if m else ""
    allowed, forbidden = set(), set()
    for line in span.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        name_cell, status = cells[0], cells[-1]
        if name_cell.lower() == "certification" or set(name_cell) <= set("-: "):
            continue
        head = re.split(r"[‐-―/]|\s-\s", name_cell)[0]
        variants = {_canon_cert(name_cell), _canon_cert(head)}
        variants = {v for v in variants if v}
        st = status.lower()
        if "not held" in st:
            forbidden |= variants
        elif "confirmed" in st:
            allowed |= variants
    return allowed, forbidden


def _profile_employment(profile: str) -> dict:
    """company(lower) -> set of raw date tokens the profile confirms for it."""
    emp = {}
    # §4 spans from "## 4. Employment history" to "## 5."
    m = re.search(r"## 4\. Employment history.*?(?=\n## 5\.)", profile, re.S)
    span = m.group(0) if m else profile
    for company in ("INRIX", "IHME", "Syntel", "American Express", "S5 Solutions",
                    "University of Washington"):
        block = re.search(re.escape(company) + r".{0,400}", span, re.S)
        if not block:
            continue
        toks = set(re.findall(r"(?:[A-Z][a-z]{2,8}\s)?(?:19|20)\d{2}", block.group(0)))
        toks |= set(re.findall(r"\bPresent\b", block.group(0)))
        if toks:
            emp[company.lower()] = toks
    return emp


def _iter_bullets(resume: dict):
    for role in resume.get("experience", []):
        for b in role.get("bullets", []):
            yield role, b


def validate_deterministic(resume: dict, contact: dict, career_profile: str,
                           job: dict, *, include_summary: bool) -> list[str]:
    issues: list[str] = []
    prof_lower = career_profile.lower()
    number_cores = _profile_number_cores(career_profile)
    edu_years = _profile_years(career_profile, ("## 2. Education",))
    cert_years = _profile_years(career_profile, ("## 3. Certifications",))

    text_fields = []
    if resume.get("summary"):
        text_fields.append(("summary", resume["summary"]))
    for role, b in _iter_bullets(resume):
        text_fields.append((f"{role.get('company')} bullet", b))
    for s in resume.get("skills", []):
        text_fields.append(("skill", s))

    # 1. banned phrases
    for where, txt in text_fields:
        low = txt.lower()
        for ph in ALWAYS_BANNED_PHRASES:
            if ph in low:
                issues.append(f"banned phrase '{ph}' in {where}")
        for ph in BANNED_PHRASES:
            if ph in low and ph not in prof_lower:
                issues.append(f"banned phrase '{ph}' in {where}")

    # 2. dashes: em dash never; en dash only inside a numeric/date range
    for where, txt in text_fields:
        if EM_DASH in txt:
            issues.append(f"em dash in {where}")
        if EN_DASH in _EN_DASH_RANGE_RE.sub("", txt):
            issues.append(f"en dash used as punctuation in {where} (allowed only in a range)")

    # 3. summary omission
    if not include_summary and resume.get("summary"):
        issues.append("summary present but was not requested (omit by default)")

    # 4. certifications — separator-insensitive match against §3 approved names;
    # forbidden names and the hard-coded NOT-held tokens still reject.
    allowed_certs, forbidden_certs = _profile_certs(career_profile)
    for c in resume.get("certifications", []):
        name = (c.get("name") or "").strip()
        low = name.lower()
        if not name:
            issues.append("certification with no name")
            continue
        cn = _canon_cert(name)
        if any(tok in low for tok in FORBIDDEN_CERT_TOKENS) or cn in forbidden_certs:
            issues.append(f"certification '{name}' is recorded in the profile as NOT held")
        elif cn not in allowed_certs:
            issues.append(f"certification '{name}' is not an approved certification in profile §3")
        if c.get("year") and c["year"] not in cert_years:
            issues.append(f"certification year '{c['year']}' for '{name}' not in profile §3")

    # 5. education years — accept a bare year, a "YYYY-YYYY" / "YYYY–YYYY" range
    # (both endpoints in §2), or a phrase like "Expected 2027" whose year(s) are
    # all in §2. The B.S. Architecture entry is "2008–2011" in the profile.
    for e in resume.get("education", []):
        yr = str(e.get("year") or "").strip()
        if yr and yr not in edu_years:
            found = set(re.findall(r"(?:19|20)\d{2}", yr))
            if not (found and found <= edu_years):
                issues.append(f"education year '{yr}' not in profile §2")
        if not (e.get("credential") and e.get("institution")):
            issues.append("education entry missing credential or institution")

    # 6. role headers + dates
    emp = _profile_employment(career_profile)
    for role in resume.get("experience", []):
        for f in ("title", "company", "location", "start"):
            if not str(role.get(f) or "").strip():
                issues.append(f"role header missing '{f}' ({role.get('company') or '?'})")
        comp = (role.get("company") or "").lower()
        known = None
        for k in emp:
            if k in comp or comp in k:
                known = emp[k]
                break
        if known is not None:
            for d in (role.get("start"), (None if role.get("current") else role.get("end"))):
                if d and not any(d in kt or kt in d for kt in known):
                    issues.append(
                        f"date '{d}' for {role.get('company')} does not match the profile's dates {sorted(known)}"
                    )
        # A role header's numbers should be dates. Flag only metric-shaped
        # tokens ($, %, 12M, N+) — a digit inside a company name ("S5") is fine.
        header_blob = " ".join(str(role.get(f, "")) for f in ("title", "company", "location"))
        for m in _METRIC_SHAPED_RE.finditer(header_blob):
            issues.append(f"metric-shaped number '{m.group(0).strip()}' in a role header")

    # 7. numbers in bullets / summary -> date or approved metric
    for where, txt in text_fields:
        for m in _NUM_RE.finditer(txt):
            tok = m.group(0).strip()
            if _YEAR_RE.fullmatch(tok):
                continue  # a bare year — checked via employment/education above
            core = _digit_core(tok)
            if core and core not in number_cores:
                issues.append(f"number '{tok}' in {where} is not in the authoritative profile")

    # 8. DO-NOT-COMBINE
    for role, b in _iter_bullets(resume):
        low = b.lower()
        comp = (role.get("company") or "").lower()
        for name, pa, pb, exc in DO_NOT_COMBINE:
            if pa(low) and pb(low):
                if exc and exc in comp:
                    continue  # documented exception (S5 bullet)
                issues.append(
                    f"bullet combines metrics the profile keeps separate ({name}); "
                    f"full bullet: {b!r}"
                )

    # 9. contact not smuggled into prose
    for where, txt in text_fields:
        if re.search(r"[\w.\-]+@[\w.\-]+\.\w+", txt) or re.search(r"\(\d{3}\)\s?\d{3}-\d{4}", txt):
            issues.append(f"contact detail embedded in {where} (contact belongs only in the name/contact line at the top of the document)")

    return issues


# --------------------------------------------------------------------------- #
# Step 5b — semantic validation (one API call). Fails closed.
# --------------------------------------------------------------------------- #
VALIDATE_SYSTEM_PROMPT = """You fact-check a finished resume against the authoritative
career profile and the target job. Report problems only; do not rewrite.

Flag any of:
- a statement, date, title, certification, skill, or metric not supported by the profile
- a metric that combines two separate profile metrics without the profile's explicit permission
- a job-description requirement presented as the candidate's experience
- language that is vague, generic, inflated, impersonal, or over-polished
- a role's dates at a precision (month+year vs. year-only) that does not
  match what the authoritative profile documents for that specific role.
  Do NOT flag differing precision across different roles as inconsistent by
  itself — one role stated to the month and another only to the year is
  correct, not an error, whenever that is what the profile documents for
  each of those roles respectively.
- a Summary section when one was not requested

Return ONLY: {"issues": [ "<short specific problem>", ... ]}
An empty list means you found nothing wrong. When unsure, include it."""


def validate_semantic(resume: dict, career_profile: str, job: dict,
                      *, include_summary: bool) -> list[str]:
    import json
    stable = f"Authoritative career profile:\n{career_profile}"
    volatile = (
        f"\n\nSummary requested: {include_summary}\n"
        f"Target job:\nTitle: {job.get('title')}\nCompany: {job.get('company')}\n"
        f"Description:\n{(job.get('description') or '')[:2500]}\n\n"
        f"Finished resume (JSON):\n{json.dumps(resume, indent=2)}"
    )
    try:
        def _call():
            return tracked_create(
                client, "resume_qc:validate",
                model=CLAUDE_MODEL, max_tokens=MAX_TOKENS,
                system=VALIDATE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": volatile},
                ]}],
            )
        with track_stage("module3_resume", stage_detail="validate"):
            response = with_retry_sync(_call)
    except Exception as e:  # fail closed — an unavailable check is an issue
        return [f"semantic validation could not run ({type(e).__name__}); not saving"]
    parsed = parse_llm_json(response.content[0].text)
    if "parse_error" in parsed or not isinstance(parsed.get("issues"), list):
        return ["semantic validation returned an unparseable result; not saving"]
    return [str(x) for x in parsed["issues"]]


# --------------------------------------------------------------------------- #
# Review report
# --------------------------------------------------------------------------- #
def write_review_report(job: dict, job_id: str, det_issues: list[str],
                        sem_issues: list[str]) -> str:
    os.makedirs(RESUME_REVIEW_DIR, exist_ok=True)
    path = os.path.join(RESUME_REVIEW_DIR, f"{job_id}.review.md")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Resume review report — {job_id}",
        "",
        f"- Generated: {now}",
        f"- Job: {job.get('title')} @ {job.get('company')} ({job.get('location') or 'location n/a'})",
        f"- Status: **BLOCKED** — no resume was saved.",
        "",
        "The draft could not be validated against "
        "`data/career_profile_source_of_truth.md`. Resolve the items below "
        "(update the authoritative profile if a fact is genuinely confirmed, or "
        "adjust expectations), then re-run:",
        "",
        f"```\npython3 main.py resume-one {job_id}\n```",
        "",
    ]
    if det_issues:
        lines += ["## Deterministic checks", ""]
        lines += [f"{i}. {msg}" for i, msg in enumerate(det_issues, 1)]
        lines += [""]
    if sem_issues:
        lines += ["## Semantic review", ""]
        lines += [f"{i}. {msg}" for i, msg in enumerate(sem_issues, 1)]
        lines += [""]
    lines += [
        "## Questions to answer",
        "",
        "- Is each flagged fact actually supported by the authoritative profile? "
        "If yes, where (section/line)? If it belongs in the profile but is missing, "
        "it must be added there first — this path will not invent it.",
        "- For any flagged metric: is it a single profile metric stated exactly, or "
        "were two combined? Only the profile's documented exceptions may combine.",
        "- For any flagged requirement: is it the candidate's real experience, or the "
        "job's ask restated?",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# --------------------------------------------------------------------------- #
# Rendered-file structural validation
# --------------------------------------------------------------------------- #
def _validate_rendered_docx(path: str, *, include_summary: bool) -> list[str]:
    issues = []
    if not resume_docx.opens_cleanly(path):
        return ["rendered .docx could not be reopened"]
    s = resume_docx.inspect_structure(path)
    if s["tables"]:
        issues.append(f"{s['tables']} table(s) in the .docx (ATS: none allowed)")
    if s["images"]:
        issues.append(f"{s['images']} image/drawing element(s) in the .docx")
    if s["text_boxes"]:
        issues.append(f"{s['text_boxes']} text box(es) in the .docx")
    if s["has_header_content"]:
        issues.append("the .docx has header content")
    if s["has_footer_content"]:
        issues.append("the .docx has footer content")
    if s["columns"] != 1:
        issues.append(f"the .docx has {s['columns']} columns (ATS: one)")
    expected = (resume_docx.S.SECTION_ORDER_WITH_SUMMARY if include_summary
                else resume_docx.S.SECTION_ORDER_NO_SUMMARY)
    got = [h for h in s["section_headings"] if h in set(expected)]
    if got != expected:
        issues.append(f"section order is {got}, expected {expected}")
    return issues


# --------------------------------------------------------------------------- #
# Canonical entry point — the ONLY resume generator.
# --------------------------------------------------------------------------- #
def generate_resume_for_job(job_id: str, *, include_summary: bool = False,
                            replace: bool = False) -> dict:
    job = resolve_job(job_id)                          # no API call
    final_path = os.path.join(RESUME_OUTPUT_DIR, f"{job_id}.docx")

    if os.path.exists(final_path) and not replace:
        raise FinalExists(
            f"a final resume already exists: {final_path}\n"
            f"To intentionally regenerate, re-run with --replace "
            f"(the current file is moved to {RESUME_SUPERSEDED_DIR}/ first)."
        )

    if resume_docx is None:
        raise MissingInput(
            "python-docx is not installed for this interpreter. "
            "Install it into .venv:  .venv/bin/pip install -r requirements.txt"
        )

    career_profile = load_career_profile()            # no API call
    contact = load_contact_block()                     # no API call, never logged

    raw = tailor_resume(job, career_profile, include_summary=include_summary)   # API 1
    resume = _loose_json_object(raw)
    if resume is None:
        raise RenderFailure("tailoring step did not return valid JSON")

    resume = authenticity_and_format_pass(resume, career_profile)              # API 2
    resume.pop("contact", None)
    if not include_summary:
        resume["summary"] = None

    det_issues = validate_deterministic(
        resume, contact, career_profile, job, include_summary=include_summary
    )
    sem_issues = []
    if not det_issues:
        sem_issues = validate_semantic(                                        # API 3
            resume, career_profile, job, include_summary=include_summary
        )

    if det_issues or sem_issues:
        review_path = write_review_report(job, job_id, det_issues, sem_issues)
        return {"status": "blocked", "job_id": job_id, "review_path": review_path,
                "issues": det_issues + sem_issues}

    # --- render to a temp file, verify, then promote ---
    os.makedirs(RESUME_BUILD_TMP_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".docx", prefix=f"{job_id}.",
                                    dir=RESUME_BUILD_TMP_DIR)
    os.close(fd)
    held_old = None
    try:
        resume_docx.render(resume, contact, tmp_path, include_summary=include_summary)
        render_issues = _validate_rendered_docx(tmp_path, include_summary=include_summary)
        if render_issues:
            review_path = write_review_report(job, job_id, render_issues, [])
            return {"status": "blocked", "job_id": job_id, "review_path": review_path,
                    "issues": render_issues}

        if os.path.exists(final_path):  # replace=True path
            os.makedirs(RESUME_SUPERSEDED_DIR, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            held_old = os.path.join(RESUME_SUPERSEDED_DIR, f"{job_id}__{stamp}.docx")
            shutil.move(final_path, held_old)

        os.makedirs(RESUME_OUTPUT_DIR, exist_ok=True)
        os.replace(tmp_path, final_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if held_old and os.path.exists(held_old) and not os.path.exists(final_path):
            shutil.move(held_old, final_path)  # restore — never leave the user with nothing
        raise RenderFailure(f"could not produce the .docx: {type(e).__name__}: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # a prior review report is now resolved
    stale = os.path.join(RESUME_REVIEW_DIR, f"{job_id}.review.md")
    if os.path.exists(stale):
        os.makedirs(RESUME_REVIEW_RESOLVED_DIR, exist_ok=True)
        shutil.move(stale, os.path.join(RESUME_REVIEW_RESOLVED_DIR, f"{job_id}.review.md"))

    return {"status": "saved", "job_id": job_id, "path": final_path,
            "superseded": held_old}


if __name__ == "__main__":
    print("modules/module3_resume.py is not a standalone entry point.")
    print("Generate one resume:  python3 main.py resume-one <job-id>")
    print("List eligible jobs:   python3 main.py resume-list")
    raise SystemExit(2)
