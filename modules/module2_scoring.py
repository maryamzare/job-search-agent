"""
Module 2 — Job Scoring
Uses Claude to score each discovered job against Marmar's profile.
Filters out jobs below MIN_FIT_SCORE.
"""

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS, MIN_FIT_SCORE,
    JOB_QUEUE_PATH, MASTER_RESUME_PATH, CANDIDATE_PROFILE,
)
from modules.util import load_queue, save_queue, parse_llm_json, get_client, tracked_create

client = get_client(ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a job-fit analyst. Score the candidate profile against the job \
description and return JSON:
  - score: integer 0-100 representing fit
  - reasons: list of 3 brief strings explaining the score
  - gaps: list of up to 3 skill/experience gaps

Respond ONLY with valid JSON — no explanation outside it."""


def load_resume() -> str:
    with open(MASTER_RESUME_PATH) as f:
        return f.read()


def score_job(job: dict) -> dict:
    description = job.get("description") or job.get("title", "")
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

    response = tracked_create(
        client, "score_job",
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    result = parse_llm_json(response.content[0].text)
    if "parse_error" in result:
        result = {"score": 0, "reasons": ["parse error"], "gaps": []}

    return result


def score_all_discovered() -> None:
    queue = load_queue(JOB_QUEUE_PATH)

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

        save_queue(queue, JOB_QUEUE_PATH)

    shortlisted = sum(1 for j in queue["jobs"] if j.get("status") == "shortlisted")
    print(f"[scoring] {shortlisted} jobs shortlisted")


if __name__ == "__main__":
    score_all_discovered()
