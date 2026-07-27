# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Changed

- **Retry policy now distinguishes rate limiting from quota exhaustion.** The first fix for the retry-everything bug (below) deliberately excluded HTTP 429 from the retryable set entirely, to keep that fix minimal. On review, that conflated two failure modes that need opposite handling: a 429 rate limit clears in seconds to minutes and should be retried; account-level quota exhaustion clears at a specific future date and retrying it is pointless. `modules/util.py` now exposes `classify_error()` with four categories — `rate_limited` (retry with backoff, honoring a `Retry-After` header when present), `quota_exceeded` (fail fast, clear notification, never retried), `transient` (network/timeout/5xx, retry with backoff), `non_retryable` (auth/permission/invalid-request/not-found, fail fast) — with quota-exhaustion message content checked before status code, so a quota cap reported via an unexpected status still classifies correctly. See `ARCHITECTURE.md` → Retry Policy for the full design, including why `with_retry` doesn't call `modules/eval_recovery.py` directly.

### Fixed

- **Retry logic no longer retries non-retryable errors.** `module3b_resume_board`'s retry wrapper previously caught the broad `anthropic.APIStatusError` base class, so a real quota-exceeded response (HTTP 400) was retried 5 times with exponential backoff before failing anyway — wasted time on a failure that retrying can never fix. Quota-exceeded, authentication failures, and other genuinely non-retryable 4xx errors now fail on the first attempt with no delay (see the retry-policy entry above for how rate limits and quota exhaustion are now told apart).
- Board-review reviewers (module2b, module3b) are now grounded in the real current date, fixing a false "future end date" flag that was triggering unnecessary resume rewrites on ~95% of reviewed resumes.
- Board decision (apply/defer/skip) now actually changes a job's status; previously the advisory board computed a verdict the rest of the pipeline ignored.
- Apply step now prefers the board-reviewed resume (`resume_v2_path`) over the pre-review draft.
- `score_job`'s description fallback now triggers on an empty string, not only a missing key.
- Stale "Georgetown, 2026" corrected to "2027" in the scoring and cover-letter prompts.
- `VALID_STATUSES`/`STATUS_EMOJI` now include `board_approved`, `in_progress`, `questionnaire_submitted`, and `closed` — statuses already in real use that the tracker previously couldn't display or accept.

### Added

- **Pipeline timing instrumentation** (`modules/lifecycle_metrics.py`, `pipeline_timing_report.py`): three new job-lifecycle timestamps — `shortlisted_at` (module2_scoring), `application_submitted_at` (module5_apply), `closed_or_expired_at` (module6_tracker) — set additively alongside the existing status/date fields, plus `python3 pipeline_timing_report.py`, which computes average/median shortlist-to-apply time, count of shortlisted jobs never applied, count of jobs closed before application, and high-score jobs delayed past a configurable threshold (default 80 score / 48h), and writes a markdown report to `outputs/reports/`. Status-based metrics work immediately against the existing 294-job queue even though none of it has the new timestamps yet — running the report today already surfaces the exact real incidents CLAUDE.md's session notes described (two Uber 88-scorers and an Apple 88-score EPM role, all closed before ever being applied to). See `ARCHITECTURE.md` → Pipeline Timing Instrumentation for the graceful-degradation design and why average/median read "no data yet" for now.
- **Evaluation recovery workflow** (`modules/eval_recovery.py`, `evaluation_recovery.py`): when a live evaluation re-run fails because the Anthropic account is over its usage quota, this now persists the failed attempt (target, "before" snapshot, and the reset time parsed straight out of Anthropic's error message) to `data/eval_recovery_state.json`, and `python3 evaluation_recovery.py check-and-run` — safe to call on a schedule (cron or otherwise) — automatically re-runs the evaluation once the quota resets and writes a before/after comparison report to `outputs/eval_reports/`. See `ARCHITECTURE.md` → Evaluation Recovery Workflow for the state machine and scheduling instructions. Seeded with the real state for the currently-blocked date-grounding-fix verification (reset time 2026-08-01).
- `modules/util.py`: shared slugify / JSON-parsing / queue-I/O / retry / date-context helpers, replacing ~9 duplicated implementations across the codebase. `load_queue`/`save_queue` are now thin wrappers over generic `load_json`/`save_json`, so other state (like the recovery workflow's) gets the same atomic-write guarantee for free.
- `job_queue.json` is now written atomically (temp file + rename) and saved incrementally during batch operations, so a crash mid-run loses at most one in-flight item instead of the whole batch.
- `tracked_create` / `tracked_create_async`: every Claude API call now logs latency, token usage, and estimated cost to `data/llm_usage_log.jsonl`; `usage_report.py` summarizes it per call type.
- `tests/test_retry.py`, `tests/test_eval_recovery.py`, `tests/test_lifecycle_metrics.py`: first automated tests in this project (79 tests total).
- `CHANGELOG.md`, `ARCHITECTURE.md` (this file and its companion).

### Removed

- Unused `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`, `SERP_API_KEY` config vars and `playwright` / `rich` / `typer` dependencies.
- Dead `sync_client` in `module3b_resume_board.py`.
