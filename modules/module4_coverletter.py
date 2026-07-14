"""
Module 4 — Cover Letter Generation
Uses Claude to write a tailored cover letter for a specific job.
Saves result to outputs/cover_letters/<slug>.txt
"""

import os
import re
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_TOKENS, MASTER_RESUME_PATH, COVERLETTER_OUTPUT_DIR

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are an expert cover letter writer for senior tech roles.
Write a compelling, concise cover letter (3-4 paragraphs, ~300 words) for the candidate.
Rules:
- Open with a specific hook referencing the company/role — never "I am writing to apply..."
- Paragraph 2: connect 2-3 of Marmar's strongest achievements to the role's core needs
- Paragraph 3: demonstrate genuine interest in this company's specific work
- Close: confident call to action, no clichés
- Voice: direct, confident, senior — not eager or generic
- Do not repeat the resume; synthesize and contextualize"""

CANDIDATE_CONTEXT = """
Candidate: Marmar
Background: Senior TPM, 8+ years. B.S. CS/SWE (UW), M.S. AI Management (Georgetown, 2026).
Certified: PMP, SAFe, CSM.
Specializes in: AI/ML programs, healthtech, data platforms, enterprise SaaS.
Located in Seattle, WA.
"""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def generate_cover_letter(job: dict) -> str:
    with open(MASTER_RESUME_PATH) as f:
        resume = f.read()

    prompt = f"""Candidate context:
{CANDIDATE_CONTEXT}

Resume highlights:
{resume[:2000]}

Job:
Title: {job.get('title')}
Company: {job.get('company')}
Description:
{job.get('description', '')[:2500]}

Write the cover letter."""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text


def save_cover_letter(job: dict, content: str) -> str:
    slug = slugify(f"{job.get('company', 'company')}_{job.get('title', 'role')}")
    filename = f"{slug}.txt"
    path = os.path.join(COVERLETTER_OUTPUT_DIR, filename)
    os.makedirs(COVERLETTER_OUTPUT_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    print(f"[coverletter] Saved: {path}")
    return path


def generate_and_save(job: dict) -> str:
    slug = slugify(f"{job.get('company', 'company')}_{job.get('title', 'role')}")
    path = os.path.join(COVERLETTER_OUTPUT_DIR, f"{slug}.txt")
    if os.path.exists(path):
        print(f"[coverletter] Skipping (already exists): {path}")
        return path
    print(f"[coverletter] Writing for: {job.get('title')} @ {job.get('company')}")
    content = generate_cover_letter(job)
    return save_cover_letter(job, content)


if __name__ == "__main__":
    import json
    from config import JOB_QUEUE_PATH

    with open(JOB_QUEUE_PATH) as f:
        queue = json.load(f)

    for job in queue["jobs"]:
        if job.get("status") == "shortlisted":
            generate_and_save(job)
