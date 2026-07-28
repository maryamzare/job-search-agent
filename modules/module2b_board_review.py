"""
Module 2b — Advisory Board Review
Runs a four-reviewer board + chair synthesis on shortlisted jobs.
All four reviewers run in parallel via AsyncAnthropic.
"""

import asyncio
import json
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS, JOB_QUEUE_PATH, MASTER_RESUME_PATH
from modules.util import (
    load_queue, save_queue, parse_llm_json, get_async_client, tracked_create_async,
    current_date_context, track_stage,
)

async_client = get_async_client(ANTHROPIC_API_KEY)

BOARD = {
    "fit_reviewer": """You are evaluating job-resume fit.
Given a job posting and resume, return JSON:
{"score": 0-10, "verdict": "strong|moderate|weak",
 "key_matches": [...], "gaps": [...]}""",

    "strategy_reviewer": """You are a career strategist.
Evaluate if this role advances the candidate's career arc.
Return JSON: {"score": 0-10, "verdict": "...",
"career_signal": "up|lateral|down", "rationale": "..."}""",

    "risk_reviewer": """You are a risk analyst reviewing job postings.
Flag red flags: vague comp, culture issues, unstable co,
unrealistic scope. Return JSON: {"score": 0-10,
"flags": [...], "verdict": "proceed|caution|skip"}""",

    "effort_reviewer": """You review application effort vs. payoff.
Is this role worth the customization time given its priority score?
Return JSON: {"score": 0-10, "effort": "low|medium|high",
"priority_fit": "yes|no", "recommendation": "..."}""",
}

CHAIR_PROMPT = """You are the Chair of a hiring advisory board.
Four reviewers assessed this job. Synthesize into a final
recommendation with a composite score and action.
Reviews: {reviews}
Return JSON: {{"composite_score": 0-10, "action": "apply|defer|skip",
"summary": "2 sentences", "top_concern": "...", "top_strength": "..."}}"""

# The only fields a reviewer needs to judge a *posting* - not the pipeline's
# accumulated history of that job (fit_score/fit_reasons/status/board_reviews/
# resume_board/resume_scorecard/...), which grows over the job's lifecycle and
# has nothing to do with fit/strategy/risk/effort. A fixed key set (defaulting
# missing ones to "") also keeps the serialized shape identical across every
# job, rather than varying with however much history a given job happens to
# carry - see ARCHITECTURE.md "Module 2b prompt context" for why that matters.
POSTING_FIELDS = ("title", "company", "location", "url", "description")


def _posting_context(job: dict) -> dict:
    """Extract just the posting fields a board reviewer needs from `job`."""
    return {field: job.get(field, "") for field in POSTING_FIELDS}


async def _call_reviewer(role: str, system_prompt: str, content: str) -> tuple[str, dict]:
    response = await tracked_create_async(
        async_client, f"board_review:{role}",
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )
    return role, parse_llm_json(response.content[0].text)


async def run_advisory_board(job: dict, resume: str) -> dict:
    with track_stage("module2b_board_review", company=job.get("company"), title=job.get("title")):
        posting = _posting_context(job)
        content = f"{current_date_context()}\n\nJOB:\n{json.dumps(posting, indent=2)}\n\nRESUME:\n{resume}"

        # All four reviewers run in parallel
        results = await asyncio.gather(
            *[_call_reviewer(role, prompt, content) for role, prompt in BOARD.items()]
        )
        reviews = dict(results)

        # Chair synthesizes
        chair_response = await tracked_create_async(
            async_client, "board_review:chair",
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": CHAIR_PROMPT.format(reviews=json.dumps(reviews, indent=2)),
            }],
        )
        board_decision = parse_llm_json(chair_response.content[0].text)

        return {"reviews": reviews, "board_decision": board_decision}


def _apply_board_decision(job: dict, decision: dict) -> None:
    """Advance the job's status based on the board's verdict.

    apply -> board_approved (unlocks resume/coverletter/apply steps)
    skip  -> filtered_out (same terminal state as a low fit score)
    defer -> left as "shortlisted" for a human to decide; the board_decision
             fields are still saved so the reasoning is visible in the tracker.
    """
    action = decision.get("action")
    if action == "apply":
        job["status"] = "board_approved"
    elif action == "skip":
        job["status"] = "filtered_out"


def review_shortlisted(statuses: list = None) -> None:
    if statuses is None:
        statuses = ["shortlisted"]

    with open(MASTER_RESUME_PATH) as f:
        resume = f.read()

    queue = load_queue(JOB_QUEUE_PATH)

    updated = 0
    for job in queue["jobs"]:
        if job.get("status") not in statuses:
            continue
        if job.get("board_decision"):
            print(f"[board] Skipping (already reviewed): {job.get('title')} @ {job.get('company')}")
            continue

        print(f"[board] Reviewing: {job.get('title')} @ {job.get('company')}")
        result = asyncio.run(run_advisory_board(job, resume))
        job["board_reviews"] = result["reviews"]
        job["board_decision"] = result["board_decision"]

        decision = result["board_decision"]
        _apply_board_decision(job, decision)
        print(
            f"  → action={decision.get('action')}  status={job.get('status')}  "
            f"score={decision.get('composite_score')}  "
            f"strength: {decision.get('top_strength', '')[:60]}"
        )
        updated += 1
        save_queue(queue, JOB_QUEUE_PATH)

    print(f"[board] Board review complete — {updated} jobs reviewed")


if __name__ == "__main__":
    review_shortlisted()
