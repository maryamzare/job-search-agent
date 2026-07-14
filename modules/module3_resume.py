"""
Module 3 — Resume Tailoring
Uses Claude to rewrite master_resume.txt for a specific job posting.
Saves result to outputs/tailored_resumes/<slug>.txt
"""

import os
import re
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS, MASTER_RESUME_PATH, RESUME_OUTPUT_DIR

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are an expert resume writer specializing in TPM, AI Product, and Engineering Manager roles.
Rewrite the candidate's resume to best match the job description provided.

RULES — follow all of these exactly:

1. ATS KEYWORDS: Surface the exact keywords from the job description in bullets and the skills section. Match recruiter search terms verbatim — do not paraphrase (e.g., if JD says "roadmap planning", use "roadmap planning", not "planning roadmaps").

2. GOOGLE XYZ FORMULA: Rewrite every bullet as "Accomplished X, as measured by Y, by doing Z."
   Good: "Reduced map release cycle time by 60%, cutting biannual releases from 6 weeks to 2.5 weeks, by redesigning ingestion pipeline architecture across 17 countries."
   Bad: "Led map modernization initiatives that improved efficiency."

3. 10-SECOND SCAN TEST: Remove any bullet, sentence, or section a hiring manager would skip. Keep only: measurable outcomes, role-relevant skills, named technologies, and scope signals (budget, team size, stakeholder count, geographic scale). Delete: vague verbs (led, worked on, supported, helped), generic claims (excellent communicator, team player), filler phrases with no data.

4. RELEVANCE FIRST: Within each role, put the most JD-relevant bullets first. Do not default to the original bullet order.

5. FACTS ONLY: Do not invent experience, technologies, or metrics. Do not claim domain expertise the candidate does not have.

6. FORMAT: Plain text, section headers in ALL CAPS, approximately 600-700 words total."""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def tailor_resume(job: dict) -> str:
    with open(MASTER_RESUME_PATH) as f:
        master = f.read()

    prompt = f"""Job to target:
Title: {job.get('title')}
Company: {job.get('company')}
Description:
{job.get('description', '')[:3000]}

Master resume:
{master}

Rewrite the resume to best match this job."""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def save_tailored_resume(job: dict, content: str) -> str:
    slug = slugify(f"{job.get('company', 'company')}_{job.get('title', 'role')}")
    filename = f"{slug}.txt"
    path = os.path.join(RESUME_OUTPUT_DIR, filename)
    os.makedirs(RESUME_OUTPUT_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    print(f"[resume] Saved: {path}")
    return path


def tailor_and_save(job: dict) -> str:
    slug = slugify(f"{job.get('company', 'company')}_{job.get('title', 'role')}")
    path = os.path.join(RESUME_OUTPUT_DIR, f"{slug}.txt")
    if os.path.exists(path):
        print(f"[resume] Skipping (already exists): {path}")
        return path
    print(f"[resume] Tailoring for: {job.get('title')} @ {job.get('company')}")
    content = tailor_resume(job)
    return save_tailored_resume(job, content)


if __name__ == "__main__":
    import json
    from config import JOB_QUEUE_PATH

    with open(JOB_QUEUE_PATH) as f:
        queue = json.load(f)

    for job in queue["jobs"]:
        if job.get("status") == "shortlisted":
            tailor_and_save(job)
