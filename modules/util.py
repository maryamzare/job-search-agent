"""
Shared helpers used across discovery, scoring, board review, and output modules.

Centralizes three things that were previously copy-pasted across the codebase:
  - slugify: company/title -> filesystem-safe slug (was duplicated in 6 files)
  - parse_llm_json: strip markdown fences and parse a JSON object from a Claude
    response (was duplicated in 3 files with drifting fallback shapes)
  - load_queue/save_queue: read/write data/job_queue.json (was duplicated in
    9+ files, none of which wrote atomically)
"""

import json
import os
import re


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def parse_llm_json(text: str) -> dict:
    """Strip ```json ... ``` fences (if present) and parse a JSON object.

    Returns {"parse_error": <first 200 chars>} if the text isn't valid JSON,
    so callers can detect failure without a try/except at every call site.
    """
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"parse_error": text[:200]}


def load_queue(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def save_queue(queue: dict, path: str) -> None:
    """Write the queue atomically (temp file + rename) so a crash mid-write
    can't truncate/corrupt the on-disk queue."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(queue, f, indent=2)
    os.replace(tmp_path, path)
