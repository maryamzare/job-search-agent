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

# HTTP status codes worth retrying with plain exponential backoff: transient
# server-side failures where a second attempt has a real chance of
# succeeding. 429 (rate limit) is deliberately handled as its own category
# (ERROR_RATE_LIMITED, below) rather than folded in here, because it gets
# different treatment: a Retry-After header, when present, should be
# honored instead of blind exponential backoff.
TRANSIENT_HTTP_STATUS_CODES = {500, 502, 503, 504}

# Error classification returned by classify_error().
ERROR_RATE_LIMITED = "rate_limited"      # HTTP 429 - retry, respecting Retry-After
ERROR_QUOTA_EXCEEDED = "quota_exceeded"  # account-level cap - fail fast, notify
ERROR_TRANSIENT = "transient"            # network/timeout/5xx - retry, exponential backoff
ERROR_NON_RETRYABLE = "non_retryable"    # auth/permission/invalid-request/etc - fail fast

# Matches Anthropic's account-level usage-cap message, e.g. "You have
# reached your specified API usage limits. You will regain access on
# 2026-08-01 at 00:00 UTC." This is free text inside a 400
# invalid_request_error, not a distinct status code or error type, and it
# is NOT the same condition as a 429 rate limit: a rate limit clears in
# seconds to minutes, this doesn't clear until a specific future
# wall-clock time (hours to days away), so retrying it is never useful.
_QUOTA_EXCEEDED_PATTERN = re.compile(r"usage limit|regain access", re.IGNORECASE)

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


def classify_error(exc: Exception) -> str:
    """Classify an exception into one of four retry categories. See the
    ERROR_* constants above for what each category means and how
    with_retry treats it.

    Order matters: the quota-exceeded message check runs before the 429
    check, so a response that happens to carry both HTTP 429 and
    quota-exhaustion language is classified as quota_exceeded, not
    rate_limited. In practice Anthropic reports quota exhaustion as a 400,
    not a 429, but checking message content first is the safer choice
    regardless of status code - the two conditions look superficially
    similar ("too many requests") but need opposite handling, so getting
    this classification wrong in either direction is costly: misclassifying
    quota exhaustion as rate-limited wastes retries on something that
    can't resolve in seconds; misclassifying a real rate limit as quota
    exhaustion abandons a request that a short wait would have recovered.
    """
    if isinstance(exc, anthropic.APIStatusError) and _QUOTA_EXCEEDED_PATTERN.search(str(exc)):
        return ERROR_QUOTA_EXCEEDED
    if isinstance(exc, anthropic.RateLimitError):
        return ERROR_RATE_LIMITED
    # anthropic.APITimeoutError is a subclass of APIConnectionError, so this
    # one check covers both network errors and timeouts.
    if isinstance(exc, anthropic.APIConnectionError):
        return ERROR_TRANSIENT
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code in TRANSIENT_HTTP_STATUS_CODES:
        return ERROR_TRANSIENT
    return ERROR_NON_RETRYABLE


def is_transient_error(exc: Exception) -> bool:
    """True for anything with_retry will retry: a genuine rate limit (429),
    a network failure/timeout, or a 500/502/503/504 server error. False for
    quota exhaustion and any other non-retryable error (auth failure,
    invalid request, permission error, not found, etc.) - retrying those
    can never succeed, so retrying them only burns time.
    """
    return classify_error(exc) in (ERROR_RATE_LIMITED, ERROR_TRANSIENT)


def is_quota_exceeded_error(exc: Exception) -> bool:
    """True if this error indicates account-level quota exhaustion (see
    classify_error's docstring for why this is checked separately from a
    429 rate limit). Exposed as its own predicate so callers - like
    modules/eval_recovery.py - can distinguish "the account is out of
    quota until a known future time" from every other failure mode without
    re-implementing the message-matching logic.
    """
    return classify_error(exc) == ERROR_QUOTA_EXCEEDED


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract a Retry-After value (in seconds) from a rate-limit
    response, if the response carries one and it's a plain number.
    Returns None if there's no header, or its value isn't numeric (this
    codebase only needs the seconds form Anthropic actually sends, not
    the HTTP-date form the spec also allows), so the caller can fall back
    to exponential backoff.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


async def with_retry(coro_fn, retries: int = 3, base_delay: float = 2.0, max_delay: float = 60.0):
    """Retry an async callable according to classify_error's category:

    - rate_limited (HTTP 429): retried with backoff. If the response
      carries a Retry-After header, that value is honored instead of
      exponential backoff (still capped at max_delay, since an API-supplied
      wait time is untrusted input and an unbounded sleep would silently
      stall the whole batch this call is part of).
    - transient (network error, timeout, 5xx): retried with exponential
      backoff (base_delay * 2**attempt, capped at max_delay).
    - quota_exceeded (account-level usage cap): never retried. Raised
      immediately with a clear notification printed - this cannot resolve
      within a retry loop's lifetime (hours to days, not seconds), so
      retrying would only hide the real problem. The exception is
      re-raised unmodified specifically so callers can catch it and
      persist recovery state - see modules/eval_recovery.py, whose
      run_recovery_check() already does exactly this when a live
      evaluation re-run raises a quota error: this is *why* with_retry
      never swallows it.
    - non_retryable (auth failure, invalid request, permission error, not
      found, etc.): never retried, raised immediately with no special
      notification beyond the exception itself - these are bugs or
      misconfiguration, not a rate/quota condition.
    """
    for attempt in range(retries):
        try:
            return await coro_fn()
        except Exception as e:
            category = classify_error(e)

            if category == ERROR_QUOTA_EXCEEDED:
                print(
                    f"    [quota-exceeded] {type(e).__name__}: the account has hit its usage "
                    f"limit. This will not resolve by retrying. Not retrying - see "
                    f"`python3 evaluation_recovery.py status` if this call is part of the "
                    f"evaluation recovery workflow, or check the account's usage dashboard "
                    f"otherwise. Error: {e}"
                )
                raise

            if category == ERROR_NON_RETRYABLE:
                raise

            if attempt == retries - 1:
                raise

            if category == ERROR_RATE_LIMITED:
                retry_after = _retry_after_seconds(e)
                if retry_after is not None:
                    delay = min(retry_after, max_delay)
                    print(f"    [retry] {type(e).__name__} (rate limited) — honoring Retry-After: "
                          f"{delay:.0f}s ({attempt + 1}/{retries})")
                else:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    print(f"    [retry] {type(e).__name__} (rate limited, no Retry-After header) "
                          f"— retrying in {delay:.0f}s ({attempt + 1}/{retries})")
            else:
                delay = min(base_delay * (2 ** attempt), max_delay)
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
