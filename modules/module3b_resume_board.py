"""
Module 3b — Resume Advisory Board
Runs 5 specialist reviewers in parallel, then a Chief Editor pass.
If the resume isn't ready to submit, triggers a rewrite pass automatically.
"""

import asyncio
import json
import os
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS, JOB_QUEUE_PATH, RESUME_OUTPUT_DIR
from modules.util import (
    slugify as _slugify, load_queue, save_queue, parse_llm_json as _parse_json,
    get_async_client, tracked_create_async, current_date_context, with_retry,
)

async_client = get_async_client(ANTHROPIC_API_KEY)

RESUME_BOARD = {
    "ats_checker": """You are an ATS (Applicant Tracking System) specialist.
Analyze the resume against the job posting for:
- Keyword coverage from the job description
- Section header safety (Education, Experience, Skills — not creative names)
- No tables, columns, or headers/footers that break parsing
- File-format risks

Return JSON:
{"score": 0-10, "keyword_hits": [...], "keyword_misses": [...],
 "format_risks": [...], "rewrites": [{"original": "...", "suggested": "..."}]}""",

    "impact_reviewer": """You are an executive resume coach focused on impact language.
Review every bullet point for:
- Weak verbs (led, assisted, worked on, helped, supported → replace with drove,
  architected, delivered, reduced, scaled)
- Missing metrics (add $, %, headcount, timeline wherever plausible from context)
- Passive voice
- Vague claims with no evidence

Return JSON:
{"score": 0-10, "weak_bullets": [{"original": "...", "rewrite": "..."}],
 "missing_metrics": [...], "top_wins": [...]}""",

    "relevance_reviewer": """You are a hiring manager for the role described.
Evaluate whether the resume surfaces the most relevant experience for THIS job.
Look for:
- Strong experience buried in older roles (should be elevated)
- Recent experience that's irrelevant (should be de-emphasized)
- Skills section alignment with job requirements
- Summary/objective fit

Return JSON:
{"score": 0-10, "buried_strengths": [...], "irrelevant_emphasis": [...],
 "summary_verdict": "strong|weak|missing", "reorder_suggestions": [...]}""",

    "narrative_reviewer": """You are a career strategist reviewing resume story coherence.
Assess:
- Does the career arc make sense for this role?
- Are transitions explained or do they look like gaps/pivots?
- Does the seniority level read correctly?
- Is the candidate positioned as a TPM / AI PM / EM (whichever applies)?

Return JSON:
{"score": 0-10, "arc_verdict": "clear|confusing|mixed",
 "positioning": "on-target|off-target", "narrative_gaps": [...],
 "suggested_framing": "..."}""",

    "red_flag_reviewer": """You are a skeptical recruiter looking for resume red flags.
Flag anything that could cause a reject:
- Unsubstantiated superlatives ("world-class", "innovative thinker")
- Claims that seem inflated without evidence
- Date gaps or unexplained short tenures
- Inconsistent seniority signals
- Anything that would make a recruiter pause

Return JSON:
{"score": 0-10, "flags": [{"issue": "...", "location": "...", "fix": "..."}],
 "verdict": "clean|minor-flags|major-flags"}""",
}

CHIEF_EDITOR_PROMPT = """You are the Chief Editor of a resume review board.
Five specialists have reviewed this resume. Your job is to:
1. Triage their feedback by impact (don't apply every note — only what matters)
2. Rewrite the top 5 flagged bullets directly
3. Output the revised summary section
4. Generate a change log the candidate can review

Prioritize in this order:
  P1 — ATS keyword misses (instant reject risk)
  P2 — Impact rewrites (callback rate)
  P3 — Relevance reordering (interview fit)
  P4 — Red flags (credibility)
  P5 — Narrative tweaks (nice-to-have)

Return JSON:
{
  "composite_score": 0-10,
  "revised_summary": "...",
  "revised_bullets": [{"original": "...", "revised": "...", "reason": "..."}],
  "change_log": [{"priority": "P1|P2|P3|P4|P5", "change": "...", "rationale": "..."}],
  "ats_score": 0-10,
  "impact_score": 0-10,
  "relevance_score": 0-10,
  "narrative_score": 0-10,
  "red_flag_score": 0-10,
  "ready_to_submit": true|false,
  "blocker": null
}"""

REWRITE_PROMPT = """You are a professional resume writer.
The Chief Editor flagged this resume as NOT ready to submit.
Blocker: {blocker}

Apply ALL changes from the change log below, then return the complete rewritten resume as plain text.

Change log:
{change_log}

Revised summary to use:
{revised_summary}

Revised bullets to apply:
{revised_bullets}

Original resume:
{resume}"""


async def _call_reviewer(role: str, system_prompt: str, content: str) -> tuple:
    async def _call():
        response = await tracked_create_async(
            async_client, f"resume_board:{role}",
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
        return role, _parse_json(response.content[0].text)
    return await with_retry(_call)


async def rewrite_resume(draft_resume: str, scorecard: dict) -> str:
    prompt = REWRITE_PROMPT.format(
        blocker=scorecard.get("blocker", ""),
        change_log=json.dumps(scorecard.get("change_log", []), indent=2),
        revised_summary=scorecard.get("revised_summary", ""),
        revised_bullets=json.dumps(scorecard.get("revised_bullets", []), indent=2),
        resume=draft_resume,
    )
    async def _rewrite_call():
        return await tracked_create_async(
            async_client, "resume_board:rewrite",
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    response = await with_retry(_rewrite_call)
    return response.content[0].text.strip()


async def review_resume(draft_resume: str, job: dict) -> dict:
    context = f"{current_date_context()}\n\nJOB POSTING:\n{job.get('description', job.get('title', ''))[:2000]}\n\nRESUME:\n{draft_resume}"

    # 5 reviewers in parallel
    results = await asyncio.gather(
        *[_call_reviewer(role, prompt, context) for role, prompt in RESUME_BOARD.items()]
    )
    reviews = dict(results)

    # Chief Editor pass
    async def _editor_call():
        return await tracked_create_async(
            async_client, "resume_board:chief_editor",
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=CHIEF_EDITOR_PROMPT,
            messages=[{
                "role": "user",
                "content": f"{context}\n\nREVIEWER FEEDBACK:\n{json.dumps(reviews, indent=2)}",
            }],
        )
    editor_response = await with_retry(_editor_call)
    scorecard = _parse_json(editor_response.content[0].text)

    final_resume = draft_resume
    if not scorecard.get("ready_to_submit", True):
        print(f"    [resume-board] Not ready — rewriting. Blocker: {scorecard.get('blocker')}")
        final_resume = await rewrite_resume(draft_resume, scorecard)

    return {
        "final_resume": final_resume,
        "scorecard": scorecard,
        "raw_reviews": reviews,
    }


def _load_tailored_resume(job: dict) -> str:
    slug = _slugify(f"{job.get('company', 'company')}_{job.get('title', 'role')}")
    path = os.path.join(RESUME_OUTPUT_DIR, f"{slug}.txt")
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def _save_final_resume(job: dict, content: str) -> str:
    slug = _slugify(f"{job.get('company', 'company')}_{job.get('title', 'role')}")
    path = os.path.join(RESUME_OUTPUT_DIR, f"{slug}_v2.txt")
    with open(path, "w") as f:
        f.write(content)
    return path


def review_resumes(statuses: list = None) -> None:
    if statuses is None:
        statuses = ["shortlisted", "board_approved"]

    queue = load_queue(JOB_QUEUE_PATH)

    updated = 0
    for job in queue["jobs"]:
        if job.get("status") not in statuses:
            continue
        if job.get("resume_board"):
            print(f"[resume-board] Skipping (done): {job.get('title')} @ {job.get('company')}")
            continue

        resume = _load_tailored_resume(job)
        if not resume:
            print(f"[resume-board] No tailored resume: {job.get('title')} @ {job.get('company')}")
            continue

        print(f"[resume-board] Reviewing: {job.get('title')} @ {job.get('company')}")
        result = asyncio.run(review_resume(resume, job))

        job["resume_board"] = result["raw_reviews"]
        job["resume_scorecard"] = result["scorecard"]

        path = _save_final_resume(job, result["final_resume"])
        job["resume_v2_path"] = path

        scorecard = result["scorecard"]
        print(
            f"  → composite={scorecard.get('composite_score')}/10  "
            f"ats={scorecard.get('ats_score')}  "
            f"impact={scorecard.get('impact_score')}  "
            f"ready={scorecard.get('ready_to_submit')}"
        )
        updated += 1
        save_queue(queue, JOB_QUEUE_PATH)

    print(f"[resume-board] Done — {updated} resumes reviewed")


if __name__ == "__main__":
    review_resumes()
