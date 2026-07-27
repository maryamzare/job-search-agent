# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Fixed

- **Retry logic no longer retries non-retryable errors.** `module3b_resume_board`'s retry wrapper previously caught the broad `anthropic.APIStatusError` base class, so a real quota-exceeded response (HTTP 400) was retried 5 times with exponential backoff before failing anyway — wasted time on a failure that retrying can never fix. The retry predicate is now a strict allowlist (network errors, timeouts, HTTP 500/502/503/504 only); quota-exceeded, authentication failures, and other 4xx errors now fail on the first attempt with no delay. See `ARCHITECTURE.md` → Retry Policy for the full reasoning, including why HTTP 429 is deliberately left out of the retryable set for now.
- Board-review reviewers (module2b, module3b) are now grounded in the real current date, fixing a false "future end date" flag that was triggering unnecessary resume rewrites on ~95% of reviewed resumes.
- Board decision (apply/defer/skip) now actually changes a job's status; previously the advisory board computed a verdict the rest of the pipeline ignored.
- Apply step now prefers the board-reviewed resume (`resume_v2_path`) over the pre-review draft.
- `score_job`'s description fallback now triggers on an empty string, not only a missing key.
- Stale "Georgetown, 2026" corrected to "2027" in the scoring and cover-letter prompts.
- `VALID_STATUSES`/`STATUS_EMOJI` now include `board_approved`, `in_progress`, `questionnaire_submitted`, and `closed` — statuses already in real use that the tracker previously couldn't display or accept.

### Added

- `modules/util.py`: shared slugify / JSON-parsing / queue-I/O / retry / date-context helpers, replacing ~9 duplicated implementations across the codebase.
- `job_queue.json` is now written atomically (temp file + rename) and saved incrementally during batch operations, so a crash mid-run loses at most one in-flight item instead of the whole batch.
- `tracked_create` / `tracked_create_async`: every Claude API call now logs latency, token usage, and estimated cost to `data/llm_usage_log.jsonl`; `usage_report.py` summarizes it per call type.
- `tests/test_retry.py`: first automated tests in this project.
- `CHANGELOG.md`, `ARCHITECTURE.md` (this file and its companion).

### Removed

- Unused `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`, `SERP_API_KEY` config vars and `playwright` / `rich` / `typer` dependencies.
- Dead `sync_client` in `module3b_resume_board.py`.
