"""
Module 2 — Job Scoring
Uses Claude to score each discovered job against Marmar's profile.
Filters out jobs below MIN_FIT_SCORE.
"""

from datetime import datetime, timezone
import re

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS, MIN_FIT_SCORE,
    JOB_QUEUE_PATH, MASTER_RESUME_PATH, CANDIDATE_PROFILE,
    BIG_COMPANIES, HIGH_PAY_TC_USD,
)
from modules.util import (
    load_queue, save_queue, parse_llm_json, get_client, tracked_create, track_stage, with_retry_sync,
)

client = get_client(ANTHROPIC_API_KEY)

# Location-fit exception. The candidate profile states a Seattle/hybrid/remote
# preference, which the model otherwise (reasonably) treats as a gap for any
# onsite non-Seattle U.S. role. This carves out the specific case where that
# preference shouldn't cost fit score: a listed employer, strong skills match,
# and evidence of high annual total compensation.
LOCATION_RULE = f"""Location-fit rule — apply before scoring location as a gap:
Do NOT treat a non-Seattle U.S. location as a fit gap, and do not reduce the
score for it, when ALL of the following hold:
  - the job is based in the United States (any state, onsite or hybrid), AND
  - the company is one of: {", ".join(BIG_COMPANIES)}, AND
  - the skills/experience match is otherwise strong, AND
  - the posting provides credible evidence that annual total compensation
    reaches at least ${HIGH_PAY_TC_USD:,}. Do not infer total compensation from
    company reputation, an undisclosed salary, or the upper end of a base-pay
    range alone.
If any condition is not established, score location normally. Missing pay is
unknown, not evidence that the high-pay condition is met. Do not treat a
U.S.-remote role as a non-Seattle onsite location gap."""

SYSTEM_PROMPT = f"""You are a job-fit analyst. Score the candidate profile against the job \
description and return JSON:
  - score: integer 0-100 representing fit
  - reasons: list of 3 brief strings explaining the score
  - gaps: list of up to 3 skill/experience gaps

{LOCATION_RULE}

Respond ONLY with valid JSON — no explanation outside it."""


def load_resume() -> str:
    with open(MASTER_RESUME_PATH) as f:
        return f.read()


def _compensation_evidence(job: dict, description: str) -> str:
    stated = job.get("salary") or job.get("compensation")
    if stated:
        return str(stated)[:800]
    snippets = []
    for match in re.finditer(r"\b(?:compensation|salary|pay range|base pay|total comp)\b",
                             description, re.I):
        snippet = description[max(0, match.start() - 40):match.end() + 180]
        snippets.append(re.sub(r"\s+", " ", snippet).strip())
        if sum(map(len, snippets)) >= 800:
            break
    return " ... ".join(snippets)[:800] or "Not stated"


def score_job(job: dict) -> dict:
    with track_stage("module2_scoring", company=job.get("company"), title=job.get("title")):
        description = job.get("description") or job.get("title", "")
        resume = load_resume()

        # Split so the part that's identical on every call (profile + resume
        # excerpt) forms a cacheable prefix, with only the job-specific part
        # after it. See ARCHITECTURE.md "Prompt caching" for why the split is
        # here specifically and the current-content-size caveat.
        stable_context = f"""Candidate profile:
{CANDIDATE_PROFILE}

Resume excerpt:
{resume[:3000]}"""

        job_context = f"""

Job posting:
Title: {job.get('title')}
Company: {job.get('company')}
Location: {job.get('location') or 'Not stated'}
Compensation evidence from posting: {_compensation_evidence(job, description)}
Description:
{description[:2000]}

Score this candidate's fit for this job."""

        def _call():
            return tracked_create(
                client, "score_job",
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": stable_context, "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": job_context},
                    ],
                }],
            )

        response = with_retry_sync(_call)

        result = parse_llm_json(response.content[0].text)
        if "parse_error" in result:
            raise ValueError(f"Scoring response was not valid JSON: {result['parse_error']}")
        score = result.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
            raise ValueError(f"Scoring response has invalid score: {score!r}")

        return result


def score_all_discovered() -> None:
    queue = load_queue(JOB_QUEUE_PATH)

    for job in queue["jobs"]:
        if job.get("status") != "discovered":
            continue

        print(f"[scoring] Scoring: {job.get('title')} @ {job.get('company')}")
        try:
            result = score_job(job)
        except Exception as exc:
            # Do not turn a model/API failure into a permanent rejection.
            job["scoring_error"] = f"{type(exc).__name__}: {exc}"[:500]
            print(f"[scoring] ERROR (left discovered for retry): {job['scoring_error']}")
            save_queue(queue, JOB_QUEUE_PATH)
            continue
        job.pop("scoring_error", None)
        job["fit_score"] = result.get("score", 0)
        job["fit_reasons"] = result.get("reasons", [])
        job["fit_gaps"] = result.get("gaps", [])

        if job["fit_score"] >= MIN_FIT_SCORE:
            job["status"] = "shortlisted"
            job.setdefault("shortlisted_at", datetime.now(timezone.utc).isoformat())
        else:
            job["status"] = "filtered_out"
            print(f"[scoring] Filtered out (score {job['fit_score']})")

        save_queue(queue, JOB_QUEUE_PATH)

    shortlisted = sum(1 for j in queue["jobs"] if j.get("status") == "shortlisted")
    print(f"[scoring] {shortlisted} jobs shortlisted")


if __name__ == "__main__":
    score_all_discovered()
