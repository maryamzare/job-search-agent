"""
Shared helpers used across discovery, scoring, board review, and output modules.

Centralizes three things that were previously copy-pasted across the codebase:
  - slugify: company/title -> filesystem-safe slug (was duplicated in 6 files)
  - parse_llm_json: strip markdown fences and parse a JSON object from a Claude
    response (was duplicated in 3 files with drifting fallback shapes)
  - load_queue/save_queue: read/write data/job_queue.json (was duplicated in
    9+ files, none of which wrote atomically)

Also provides tracked_create/tracked_create_async: thin wrappers around
client.messages.create() that log latency, token usage, and estimated cost
to data/llm_usage_log.jsonl for every call, so the pipeline's real
performance/cost profile can be measured instead of guessed.
"""

import asyncio
import json
import os
import re
import time
from datetime import date

import anthropic

from config import LLM_USAGE_LOG_PATH

# HTTP status codes worth retrying: transient server-side failures where a
# second attempt has a real chance of succeeding. Deliberately excludes 429
# (rate limit) even though it's commonly retried elsewhere - if this proves
# too conservative in practice (a legitimate per-minute rate limit fails
# immediately instead of backing off), add 429 back in as a follow-up; see
# ARCHITECTURE.md for the reasoning.
TRANSIENT_HTTP_STATUS_CODES = {500, 502, 503, 504}

# $ per 1M tokens. Source: Anthropic pricing as of this writing. Update if
# CLAUDE_MODEL changes or Anthropic revises pricing.
PRICING_PER_MTOK = {
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_write_5m": 3.75,
        "cache_write_1h": 6.00,
        "cache_read": 0.30,
    },
}


def get_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def get_async_client(api_key: str) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=api_key)


def is_transient_error(exc: Exception) -> bool:
    """True only for failures worth retrying: network-level connection
    failures/timeouts, and 500/502/503/504 server errors.

    False for everything else, notably: quota-exceeded, authentication
    failures, and invalid requests - these are all non-retryable 4xx
    responses that will never succeed no matter how many times or how
    long you wait to retry, so retrying them only burns time. This was a
    real bug: a quota-exceeded response (HTTP 400, Anthropic's
    invalid_request_error type) was retried 5 times with exponential
    backoff before eventually failing anyway.
    """
    # anthropic.APITimeoutError is a subclass of APIConnectionError, so this
    # one check covers both network errors and timeouts.
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in TRANSIENT_HTTP_STATUS_CODES
    return False


async def with_retry(coro_fn, retries: int = 3, base_delay: float = 2.0):
    """Retry an async callable, but only for transient failures (see
    is_transient_error). A non-transient error (quota exceeded, auth
    failure, invalid request, any other 4xx) is raised immediately on the
    first attempt - no retry, no backoff delay.
    """
    for attempt in range(retries):
        try:
            return await coro_fn()
        except Exception as e:
            if not is_transient_error(e):
                raise
            if attempt == retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"    [retry] {type(e).__name__} — retrying in {delay:.0f}s ({attempt + 1}/{retries})")
            await asyncio.sleep(delay)


def current_date_context() -> str:
    """A one-line date-grounding string for prompts that reason about dates.

    Without this, a reviewer has no way to know "today" and can mistake a
    real past date for a future one (e.g. flagging a resume's real end date
    of April 2026 as suspiciously future-dated when today is actually
    July 2026) - this was found causing false "major-flags" verdicts on
    ~95% of resume-board reviews.
    """
    return f"Today's date is {date.today().isoformat()}."


def _estimate_cost_usd(model: str, usage) -> float | None:
    """Estimate USD cost of one API call from its response.usage.

    Returns None if the model isn't in PRICING_PER_MTOK (unpriced/unknown
    model) rather than guessing.
    """
    pricing = PRICING_PER_MTOK.get(model)
    if not pricing or usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (
        input_tokens * pricing["input"]
        + output_tokens * pricing["output"]
        + cache_creation * pricing["cache_write_5m"]  # assumes default 5m TTL
        + cache_read * pricing["cache_read"]
    ) / 1_000_000
    return round(cost, 6)


def _log_llm_call(label: str, model: str, latency_s: float, usage, success: bool, error: str = None) -> None:
    entry = {
        "timestamp": time.time(),
        "label": label,
        "model": model,
        "latency_s": round(latency_s, 3),
        "success": success,
    }
    if usage is not None:
        entry["input_tokens"] = getattr(usage, "input_tokens", None)
        entry["output_tokens"] = getattr(usage, "output_tokens", None)
        entry["cache_creation_input_tokens"] = getattr(usage, "cache_creation_input_tokens", None)
        entry["cache_read_input_tokens"] = getattr(usage, "cache_read_input_tokens", None)
        entry["cost_usd"] = _estimate_cost_usd(model, usage)
    if error is not None:
        entry["error"] = error

    log_dir = os.path.dirname(LLM_USAGE_LOG_PATH)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    with open(LLM_USAGE_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def tracked_create(client, label: str, **kwargs):
    """client.messages.create(**kwargs), instrumented with latency/cost logging.

    Transparent: returns exactly what client.messages.create() would have
    returned, and re-raises any exception after logging the failed call.
    """
    model = kwargs.get("model", "unknown")
    start = time.monotonic()
    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        _log_llm_call(label, model, time.monotonic() - start, None, False, str(e))
        raise
    _log_llm_call(label, model, time.monotonic() - start, response.usage, True)
    return response


async def tracked_create_async(client, label: str, **kwargs):
    """Async counterpart of tracked_create for AsyncAnthropic clients."""
    model = kwargs.get("model", "unknown")
    start = time.monotonic()
    try:
        response = await client.messages.create(**kwargs)
    except Exception as e:
        _log_llm_call(label, model, time.monotonic() - start, None, False, str(e))
        raise
    _log_llm_call(label, model, time.monotonic() - start, response.usage, True)
    return response


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


def load_json(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def save_json(data: dict, path: str) -> None:
    """Write atomically (temp file + rename) so a crash mid-write can't
    truncate/corrupt the file on disk."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def load_queue(path: str) -> dict:
    return load_json(path)


def save_queue(queue: dict, path: str) -> None:
    save_json(queue, path)
