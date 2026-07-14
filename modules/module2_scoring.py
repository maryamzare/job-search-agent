"""
Module 2 — Job Scoring
Uses Claude to score each discovered job against Marmar's profile.
Filters out jobs below MIN_FIT_SCORE.
"""

import json
import re
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS, MIN_FIT_SCORE, JOB_QUEUE_PATH, MASTER_RESUME_PATH

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a job-fit analyst. Given a candidate profile and a job description,
return a JSON object with:
  - score: integer 0-100 representing fit
  - reasons: list of 3 brief strings explaining the score
  - gaps: list of up to 3 skill/experience gaps

Respond ONLY with valid JSON. No explanation outside the JSON."""

CANDIDATE_PROFILE = """
Name: Marmar
Title: Senior TPM
Experience: 8+ years
Education: B.S. CS/SWE (UW), M.S. AI Management (Georgetown, 2026)
Certifications: PMP, SAFe, CSM
Target roles: Senior TPM, AI Product Manager, Engineering Manager
Industries: AI/ML, healthtech, data platforms, enterprise SaaS
Location: Seattle WA — hybrid or remote preferred
"""


def load_resume() -> str:
    with open(MASTER_RESUME_PATH) as f:
        return f.read()


def score_job(job: dict) -> dict:
    description = job.get("description", job.get("title", ""))
    resume = load_resume()

    prompt = f"""Candidate profile:
{CANDIDATE_PROFILE}

Resume excerpt:
{resume[:3000]}

Job posting:
Title: {job.get('title')}
Company: {job.get('company')}
Description:
{description[:2000]}

Score this candidate's fit for this job."""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"score": 0, "reasons": ["parse error"], "gaps": []}

    return result


def score_all_discovered() -> None:
    with open(JOB_QUEUE_PATH) as f:
        queue = json.load(f)

    for job in queue["jobs"]:
        if job.get("status") != "discovered":
            continue

        print(f"[scoring] Scoring: {job.get('title')} @ {job.get('company')}")
        result = score_job(job)
        job["fit_score"] = result.get("score", 0)
        job["fit_reasons"] = result.get("reasons", [])
        job["fit_gaps"] = result.get("gaps", [])

        if job["fit_score"] >= MIN_FIT_SCORE:
            job["status"] = "shortlisted"
        else:
            job["status"] = "filtered_out"
            print(f"[scoring] Filtered out (score {job['fit_score']})")

    with open(JOB_QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2)

    shortlisted = sum(1 for j in queue["jobs"] if j.get("status") == "shortlisted")
    print(f"[scoring] {shortlisted} jobs shortlisted")


if __name__ == "__main__":
    score_all_discovered()
