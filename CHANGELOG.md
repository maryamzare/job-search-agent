# Changelog

## 2.0.0 — Interview-throughput rewrite

- Removed the four-reviewer job board/Chair and five-reviewer resume board/Chief Editor. They duplicated the scorer and tailoring prompt without measured interview-conversion benefit.
- The default pipeline is now `discover → score → tailor resume → apply`; cover letters are generated only when requested.
- Invalid scoring JSON no longer becomes a permanent score of zero. Failed jobs remain `discovered` and retryable with an explicit `scoring_error`.
- `in_progress` applications reappear first in the application flow instead of becoming unreachable.
- Application flow blocks when the posting-specific resume is missing and saves each decision immediately.
- Generated filenames include a posting fingerprint, preventing stale materials from being reused for reposted or changed roles.
- Discovery deduplicates by posting URL instead of suppressing every future role with the same company/title.
- Fixed Lever description parsing for its public API schema.
- Manual tracker transitions now populate application and interview timestamps.

All notable changes to this project are documented here.

## [Unreleased]

### Changed

- **`discover` now searches exactly five sources, each fault-isolated.** LinkedIn
  (unchanged, keeps `LINKEDIN_LOOKBACK_HOURS`), Apple Careers, Anthropic
  (Greenhouse), OpenAI (Ashby), Oracle Careers. Every source runs through a new
  `_safe(...)` wrapper that catches any exception, logs it, and returns `[]`, so
  one source failing (HTTP error, bad JSON, changed schema) never stops the
  others. `discover` still never scores jobs or generates résumés. New tests:
  `tests/test_discovery_sources.py` (verified against the live responses).

  - **Apple** (`search_jobs_apple`): the old `POST jobs.apple.com/api/role/search`
    now 301-redirects to a not-found page. The adapter now reads the
    `jobs.apple.com/en-us/search` page's embedded `__staticRouterHydrationData`
    (`loaderData.search`), paginates via `totalRecords` (up to `APPLE_MAX_PAGES`),
    and pulls the full JD from the detail page's `loaderData.jobDetails.jobsData`
    (summary + description + minimum/preferred qualifications), falling back to
    the search summary on failure.
  - **OpenAI** (`search_jobs_ashby`, `api.ashbyhq.com/posting-api/job-board/openai`):
    OpenAI is not on Greenhouse. Ashby returns a city-only `location`, so the
    country is taken from `address.postalAddress.addressCountry` and `isRemote`
    is honored.
  - **Oracle** (`search_jobs_oracle`): the list endpoint
    (`recruitingCEJobRequisitions`, paginated by `offset`/`TotalJobsCount`)
    returns `ExternalDescriptionStr` / `ExternalResponsibilitiesStr` /
    `ExternalQualificationsStr` as `null`; the complete description is now built
    from the per-requisition **detail** endpoint
    (`recruitingCEJobRequisitionDetails`) — summary + responsibilities +
    qualifications. `ORACLE_API_BASE` + `ORACLE_SITE_NUMBER` are the only
    tenant-specific constants.
  - **`_location_matches`** no longer matches a bare `us` substring (which had
    accepted Houston, Belarus, Australia, …). It now checks explicit country
    fields, the `United States`/`USA` forms, and word-boundary US state codes.
  - **`deduplicate()`** keys on the canonical URL, matching the
    posting-identity rule in `ARCHITECTURE.md`; two requisitions with the same
    company/title but different URLs are both kept.
  - Apple and Oracle records missing a position/requisition ID are dropped and
    logged, never turned into a URL with an empty ID.

- **Resume generation is now one job at a time, validated, and produces a `.docx`.**
  `python3 main.py resume-one <job-id>` is the only resume path. It resolves
  exactly one job by an exact `job_artifact_key` match (missing / unknown /
  ineligible / ambiguous IDs exit non-zero with no API call), uses
  `data/career_profile_source_of_truth.md` as the sole factual source
  (historical `master_resume*.txt` files are not inputs to it), tailors to the
  job description, then runs an authenticity + formatting pass and
  deterministic + semantic validation. Deterministic checks cover banned
  phrases, em/en dashes, section order, default Summary omission, one-column
  structure with no tables/text boxes/images/headers/footers, date-format
  consistency, certification names and years (against profile §3), education
  years (against §2), employment dates, role-header completeness, and
  context-classified numbers (contact vs. date vs. achievement metric), plus
  the profile's DO-NOT-COMBINE pairs — with the profile's documented S5
  exception preserved. Validation fails closed: an unparseable or unavailable
  semantic result is treated as a failure. On any unresolved issue the run
  writes only `outputs/resume_reviews/<job-id>.review.md` and saves no resume.
  On success it renders one ATS-safe single-column `.docx` to a temp file that
  must reopen cleanly before being promoted to
  `outputs/tailored_resumes/<job-id>.docx`; an existing final is never
  silently overwritten (`--replace` moves it to `superseded/` first, and is
  restored if the new run fails). `--with-summary` opts the Summary section in.
- **Deterministic certification check is separator-insensitive.** It now parses
  §3 of the authoritative profile into approved / not-held name sets and
  compares a canonical form (lowercased, trailing year dropped, and commas /
  pipes / slashes / parentheses / hyphens / any Unicode dash collapsed to
  spaces), so an approved cert written "Certified Scrum Master, Scrum Alliance"
  or "AI Foundations — OpenAI Academy" matches "Certified Scrum Master —
  Scrum Alliance" in the profile. Not-held names (SAFe / SPC, Google UX
  Design) and certs absent from §3 are still rejected. The DO-NOT-COMBINE
  finding now records the full offending bullet instead of an 80-character
  prefix.
- **`python3 main.py run` stops after scoring** and prints how to pick one job.
  Bare `python3 main.py resume` prints the same guidance and exits without
  generating anything. `run_job.py` is retired and exits with instructions.
  `.docx` styling constants are frozen in `modules/resume_style.py`, measured
  read-only from `MaryamZareResume 4-21-25.docx`; that file is not a runtime
  dependency. Contact details come only from git-ignored `data/contact_block.txt`
  and are never sent to model calls or written to logs.

- **Retry policy now distinguishes rate limiting from quota exhaustion.** The first fix for the retry-everything bug (below) deliberately excluded HTTP 429 from the retryable set entirely, to keep that fix minimal. On review, that conflated two failure modes that need opposite handling: a 429 rate limit clears in seconds to minutes and should be retried; account-level quota exhaustion clears at a specific future date and retrying it is pointless. `modules/util.py` now exposes `classify_error()` with four categories — `rate_limited` (retry with backoff, honoring a `Retry-After` header when present), `quota_exceeded` (fail fast, clear notification, never retried), `transient` (network/timeout/5xx, retry with backoff), `non_retryable` (auth/permission/invalid-request/not-found, fail fast) — with quota-exhaustion message content checked before status code, so a quota cap reported via an unexpected status still classifies correctly. See `ARCHITECTURE.md` → Retry Policy for the full design, including why `with_retry` doesn't call `modules/eval_recovery.py` directly.

### Fixed

- **Retry logic no longer retries non-retryable errors.** `module3b_resume_board`'s retry wrapper previously caught the broad `anthropic.APIStatusError` base class, so a real quota-exceeded response (HTTP 400) was retried 5 times with exponential backoff before failing anyway — wasted time on a failure that retrying can never fix. Quota-exceeded, authentication failures, and other genuinely non-retryable 4xx errors now fail on the first attempt with no delay (see the retry-policy entry above for how rate limits and quota exhaustion are now told apart).
- Board-review reviewers (module2b, module3b) are now grounded in the real current date, fixing a false "future end date" flag that was triggering unnecessary resume rewrites on ~95% of reviewed resumes.
- Board decision (apply/defer/skip) now actually changes a job's status; previously the advisory board computed a verdict the rest of the pipeline ignored.
- Apply step now prefers the board-reviewed resume (`resume_v2_path`) over the pre-review draft.
- `score_job`'s description fallback now triggers on an empty string, not only a missing key.
- Stale "Georgetown, 2026" corrected to "2027" in the scoring and cover-letter prompts.
- `VALID_STATUSES`/`STATUS_EMOJI` now include `board_approved`, `in_progress`, `questionnaire_submitted`, and `closed` — statuses already in real use that the tracker previously couldn't display or accept.
- **`module2b_board_review` no longer serializes a job's full accumulated pipeline history into every reviewer's prompt.** `run_advisory_board()` previously called `json.dumps(job, indent=2)`, including fields like `board_reviews`, `resume_scorecard`, and `status` that mutate as a job moves through later stages — meaning the same job's prompt content changed over time and could balloon to ~11,600 tokens (measured) for a job carrying full history, independent of any prompt-caching work and defeating it by construction. `_posting_context(job)` now extracts only the five fields a reviewer actually needs (`title`, `company`, `location`, `url`, `description`, defaulting missing ones to `""` for a fixed shape), which measured out to a 92.7% average size reduction across the 45 already-reviewed jobs currently in the queue (48,662 → 3,553 chars/job). No prompt caching was added in this change — see `ARCHITECTURE.md` → Module 2b prompt context and `docs/PERFORMANCE_BASELINE.md` § 3 for the full before/after analysis and why this is a prerequisite for caching, not caching itself.

### Added

- **Pipeline timing instrumentation** (`modules/lifecycle_metrics.py`, `pipeline_timing_report.py`): three new job-lifecycle timestamps — `shortlisted_at` (module2_scoring), `application_submitted_at` (module5_apply), `closed_or_expired_at` (module6_tracker) — set additively alongside the existing status/date fields, plus `python3 pipeline_timing_report.py`, which computes average/median shortlist-to-apply time, count of shortlisted jobs never applied, count of jobs closed before application, and high-score jobs delayed past a configurable threshold (default 80 score / 48h), and writes a markdown report to `outputs/reports/`. Status-based metrics work immediately against the existing 294-job queue even though none of it has the new timestamps yet — running the report today already surfaces real incidents matching what prior session notes described (high-scoring jobs closed before ever being applied to). See `ARCHITECTURE.md` → Pipeline Timing Instrumentation for the graceful-degradation design and why average/median read "no data yet" for now.
- **Evaluation recovery workflow** (`modules/eval_recovery.py`, `evaluation_recovery.py`): when a live evaluation re-run fails because the Anthropic account is over its usage quota, this now persists the failed attempt (target, "before" snapshot, and the reset time parsed straight out of Anthropic's error message) to `data/eval_recovery_state.json`, and `python3 evaluation_recovery.py check-and-run` — safe to call on a schedule (cron or otherwise) — automatically re-runs the evaluation once the quota resets and writes a before/after comparison report to `outputs/eval_reports/`. See `ARCHITECTURE.md` → Evaluation Recovery Workflow for the state machine and scheduling instructions. Seeded with the real state for the currently-blocked date-grounding-fix verification (reset time 2026-08-01).
- `modules/util.py`: shared slugify / JSON-parsing / queue-I/O / retry / date-context helpers, replacing ~9 duplicated implementations across the codebase. `load_queue`/`save_queue` are now thin wrappers over generic `load_json`/`save_json`, so other state (like the recovery workflow's) gets the same atomic-write guarantee for free.
- `job_queue.json` is now written atomically (temp file + rename) and saved incrementally during batch operations, so a crash mid-run loses at most one in-flight item instead of the whole batch.
- `tracked_create` / `tracked_create_async`: every Claude API call now logs latency, token usage, and estimated cost to `data/llm_usage_log.jsonl`; `usage_report.py` summarizes it per call type.
- `tests/test_retry.py`, `tests/test_eval_recovery.py`, `tests/test_lifecycle_metrics.py`: first automated tests in this project (79 tests total).
- `CHANGELOG.md`, `ARCHITECTURE.md` (this file and its companion).

- **Performance baseline instrumentation** (`modules/performance_baseline.py`, `performance_baseline_report.py`, `docs/PERFORMANCE_BASELINE.md`): added ahead of prompt-caching work so its effect can be measured against something real instead of asserted. `modules.util.track_stage()` is a new context manager wrapping the per-job unit of work in every LLM-driving stage (`module1_discovery`, `module2_scoring`, `module2b_board_review`, `module3_resume`, `module3b_resume_board`, `module4_coverletter`), logging wall-clock duration to the new `data/pipeline_stage_log.jsonl` (`config.PIPELINE_STAGE_LOG_PATH`) — `module5_apply` and `module6_tracker` are deliberately excluded (human decision time and no-LLM-call stages aren't agent performance). `modules/performance_baseline.py` rolls both this new stage log and the existing `data/llm_usage_log.jsonl` up by module (`compute_api_usage_by_module`, `compute_stage_timing_by_module`, `slowest_modules`), and `python3 performance_baseline_report.py` prints the result. `docs/PERFORMANCE_BASELINE.md` documents the current numbers, explicit about which are structural estimates (from running real prompt-construction code against real repo data, chars÷4) versus measured data (currently zero — a pre-existing `llm_usage_log.jsonl` contaminated by this session's own mocked-test runs was found and reset first, so the baseline honestly reports "no real data yet" rather than fabricated-looking numbers) — including a newly-documented finding that `module2b_board_review`'s job-JSON-dump grows ~6x (from ~1,900 to ~11,600 tokens) once a job has accumulated prior board/resume-board history, which independently defeats prompt caching for that call regardless of caching implementation. See `ARCHITECTURE.md` → Performance Baseline Instrumentation for the full design rationale.
- **Prompt caching architecture for the three sequential single-call stages** (`module2_scoring`, `module3_resume`, `module4_coverletter`): each now sends its stable, per-job-invariant content (candidate profile and/or master resume) as a separate `cache_control: {"type": "ephemeral"}`-marked message block ahead of its volatile, job-specific content, instead of one flat prompt string. `module3_resume` also reorders master resume before the job posting — the only prompt-content change made, and required because caching needs the stable content to be a prefix. `modules/performance_baseline.py`'s per-module rollup now also sums `cache_creation_input_tokens`/`cache_read_input_tokens`. **Known limitation, verified before shipping:** at current real content sizes, none of the three modules' stable prefixes reach `claude-sonnet-4-6`'s 2048-token cache-write minimum (module3_resume closest at ~1,665 estimated tokens, ~383 short) — so this does not yet produce a measurable cache hit, cost reduction, or latency change. The architecture activates automatically with no further code changes once combined stable content grows past the threshold or the model changes to one with a lower minimum. See `ARCHITECTURE.md` → Prompt caching and `docs/PERFORMANCE_BASELINE.md` § 6 for the full analysis.
- **Retry/backoff for `module2_scoring`, `module3_resume`, `module4_coverletter`** — the three modules on the sync Anthropic client previously had no retry coverage at all; a single transient network blip, timeout, or 5xx failed the whole item outright. `modules/util.py` gains `with_retry_sync`, a synchronous twin of the existing `with_retry` sharing the same `classify_error()`-based four-category policy (retry transient/5xx and 429-rate-limited with backoff, honoring `Retry-After`; fail fast with no retry on quota exhaustion or auth/permission/invalid-request errors). All three modules now wrap their `tracked_create(...)` call through it, mirroring the `_call()`-closure pattern `module3b_resume_board` already uses for `with_retry`. Every LLM-calling module in the pipeline now has retry coverage; `module1_discovery`'s HTTP scraping remains the one call path without it (a different failure domain `classify_error` wasn't built for). See `ARCHITECTURE.md` → Retry/backoff for the synchronous modules.

### Removed

- Unused `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`, `SERP_API_KEY` config vars and `playwright` / `rich` / `typer` dependencies.
- Dead `sync_client` in `module3b_resume_board.py`.
- **Batch resume generation** (`main.py resume` looping every shortlisted/board_approved
  job) and `module3_resume.tailor_and_save()` / `save_tailored_resume()`. There is
  now one validated generation path; nothing can create a resume without going
  through it.
- **The Lever discovery adapter** (`search_jobs_lever`) and every Lever company
  (Netflix, Canva, Airtable, Vercel), plus the non-target Greenhouse companies
  (Scale AI, Figma, Notion, Stripe, Databricks, Cohere, Mistral AI, Perplexity).
  Greenhouse is retained for Anthropic only.

### Dependencies

- Added `python-docx>=1.1.0` (installed into `.venv`; pulls `lxml`). Used only by
  the `resume-one` render path — `modules/module3_resume` still imports on an
  interpreter without it, and refuses to render with a clear message.
