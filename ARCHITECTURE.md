# Architecture

## Pipeline

```
discover → score → board review → tailor resume → resume board → cover letter → apply → track
```

| Stage | Module | Responsibility |
|-------|--------|-----------------|
| 1 | `modules/module1_discovery.py` | Scrape job postings (LinkedIn guest API, Greenhouse/Lever ATS APIs) |
| 2 | `modules/module2_scoring.py` | Score each job 0-100 against the candidate profile; filter below `MIN_FIT_SCORE` |
| 2b | `modules/module2b_board_review.py` | 4-reviewer advisory board + chair synthesis; produces an apply/defer/skip verdict |
| 3 | `modules/module3_resume.py` | Tailor `master_resume.txt` to the job description |
| 3b | `modules/module3b_resume_board.py` | 5-reviewer ATS/impact/relevance/narrative/red-flag board + chief editor; auto-rewrites the resume if not ready to submit |
| 4 | `modules/module4_coverletter.py` | Generate a role-specific cover letter |
| 5 | `modules/module5_apply.py` | Guide and track application submission |
| 6 | `modules/module6_tracker.py` | Status summary and pipeline view over `job_queue.json` |

`main.py` orchestrates all stages via a CLI dict-dispatcher. `config.py` centralizes API keys, model selection, candidate profile, and file paths. `modules/util.py` centralizes cross-cutting concerns used by every module: queue I/O, Claude client construction, LLM-call instrumentation, retry policy, JSON-response parsing, and date-grounding for prompts.

## Design Decisions

### Single source of truth for job state

`data/job_queue.json` holds every job's full lifecycle — discovery data, fit score, board verdicts, resume/cover-letter paths, application status. Writes are atomic (temp file + `os.replace`) and incremental (saved after each item in a batch loop, not once at the end), so a crash mid-run costs at most one in-flight item, not the whole batch.

### Quality gates before submission

Both the job-fit board (2b) and the resume board (3b) exist to catch problems before a human applies, not to speed up throughput. The specialist-reviewers-plus-synthesizer pattern (4 or 5 narrow reviewers → a chair/chief-editor that triages their feedback) is deliberate: each reviewer stays focused on one question (ATS keywords, red flags, career-arc coherence) instead of diluting a single do-everything prompt.

### Date-grounding for reviewers

Reviewer prompts that reason qualitatively about a resume (red flags, career-arc coherence) have no inherent notion of "today" — an LLM call has no wall-clock access unless told. `modules/util.current_date_context()` is prepended to the shared reviewer context in module2b and module3b for exactly this reason: without it, a real past date can be misread as suspiciously future-dated (this was found happening on ~95% of resume-board reviews in production data, each one incorrectly flagging the candidate's real, already-past employment end date as a red flag and triggering an unnecessary rewrite).

### Retry policy: three failure categories, three different responses

**Problem.** The original retry wrapper (`module3b_resume_board._with_retry`) caught `anthropic.APIStatusError`, the base class for *every* non-2xx HTTP response — 400 (invalid request / quota exceeded), 401 (auth failure), 403, 404, and 429 alongside the 500-series errors it was actually meant to protect against. In practice, a quota-exceeded response (which Anthropic returns as an `invalid_request_error` with HTTP 400) was retried 5 times with exponential backoff before failing anyway — wasted latency on a failure mode that retrying can never resolve, since the account-level condition doesn't change between attempts a few seconds apart.

A first pass at fixing this (see git history) narrowed retryability down to a single allowlist — network errors, timeouts, and 500/502/503/504 — and deliberately excluded HTTP 429 (rate limit) entirely, reasoning that the fix should close the one verified incident without speculatively broadening scope. On review, that was too blunt an instrument: a 429 rate limit and account quota exhaustion are both "the account made too many requests," but they need *opposite* handling — a rate limit clears in seconds to minutes and should be retried; a quota cap clears at a specific future wall-clock time (hours to days away) and retrying it is pointless. Folding them into one bucket (or excluding both, as the first pass did) gets one of the two cases wrong. The current design splits them explicitly.

**Design — four categories, via `modules/util.classify_error()`:**

| Category | Examples | Behavior |
|---|---|---|
| `rate_limited` | HTTP 429 | Retry with exponential backoff; **honor a `Retry-After` header if the response carries one**, falling back to exponential backoff otherwise. Both forms are capped at `max_delay` (default 60s) since an API-supplied wait time is untrusted input. |
| `quota_exceeded` | Account-level usage cap (Anthropic reports this as free text inside a 400 `invalid_request_error`, e.g. *"...You will regain access on 2026-08-01 at 00:00 UTC."*) | **Fail fast — never retried.** A clear notification is printed immediately, and the original exception is re-raised unmodified so a caller can persist recovery state (see Evaluation Recovery Workflow, below — this is exactly what makes that workflow possible: `with_retry` never swallows this error). |
| `transient` | Network failure, timeout, HTTP 500/502/503/504 | Retry with exponential backoff (unchanged from the first pass). |
| `non_retryable` | 401 (auth failure), 403 (permission error), 400 without quota-exhaustion wording (invalid request), 404, etc. | Fail fast, no retry, no special notification beyond the exception itself — these are bugs or misconfiguration, not a rate/quota condition. |

**Classification order matters.** `classify_error()` checks for quota-exhaustion *message content* before it checks status codes or exception type — including before the 429 check. So a response that happened to carry both HTTP 429 and quota-exhaustion wording would still classify as `quota_exceeded`, not `rate_limited`. In practice Anthropic reports quota exhaustion as a 400, not a 429, so this ordering is a defensive choice rather than one that changes today's observed behavior — but getting it wrong in either direction is costly (retrying quota exhaustion wastes time on something that can't resolve in seconds; treating a real rate limit as quota exhaustion abandons a request a short wait would have recovered), so the more specific, content-based signal wins over the coarser, code-based one.

**Why "save recovery state" isn't code inside `with_retry` itself.** `modules/util.py` is a small, dependency-free leaf module (deliberately — `modules/eval_recovery.py` already imports from it, so the reverse import would be circular). Rather than reach into `eval_recovery.save_failed_eval_state()` directly — which would also require inventing a `target`/`before` for call sites that don't have one, like an ordinary pipeline run that isn't re-verifying anything — `with_retry` does the one thing that *enables* recovery-state-saving everywhere: it never retries or swallows a `quota_exceeded` error, and re-raises the exact original exception. `modules/eval_recovery.py`'s `run_recovery_check()` already relies on exactly this behavior — it catches whatever `live_rerun_fn` raises and calls `parse_quota_reset_time()` on it. The notification requirement is satisfied directly: a distinct, clearly-worded message prints immediately, before the exception propagates.

**Verification.** `tests/test_retry.py` (30 tests) proves: `classify_error()` puts every case in the table above in the right bucket, including the priority-ordering edge case (429 + quota wording → `quota_exceeded`); a 429 retries and can succeed; a 429 without a `Retry-After` header falls back to exponential backoff; a 429 *with* a `Retry-After` header uses that value instead, capped at `max_delay`; a non-numeric `Retry-After` value falls back to exponential backoff rather than erroring; quota exhaustion never retries, never sleeps, and re-raises the identical exception object; and each non-retryable case (auth, permission, generic invalid request) fails on the first attempt with zero retries and zero sleep.

### Evaluation recovery workflow

**Problem.** Verifying a prompt/behavior fix honestly requires a real live API call — comparing stored "before" data against a fresh "after" result. But a live call can be blocked by something no amount of in-process retrying fixes: an account-level usage quota that resets at a specific future timestamp (hours to days away), reported by Anthropic as free text in the error message rather than a `retry-after` header. This is a fundamentally different failure mode than the transient errors `with_retry` handles — those resolve in seconds, this one doesn't resolve until a known wall-clock time in the future, so it needs *persistence* (remember what we were trying to do and what "before" looked like) and *scheduling* (try again after that time), not a tighter retry loop.

**Design — `modules/eval_recovery.py` + `evaluation_recovery.py` (CLI).** A small state machine persisted to `data/eval_recovery_state.json` (gitignored, like the rest of `data/`):

```
pending ──(quota resets, live re-run succeeds)──> completed
   │
   └──(quota resets, live re-run hits a different, unexpected error)──> failed
```

- `save_failed_eval_state(target, before, error)` — called when a live evaluation attempt fails. Parses the reset timestamp out of the error message via regex (`regain access on (\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2}) UTC`) and persists it alongside `target` (what to re-check) and `before` (the snapshot to compare against). Returns `None` for the reset time rather than guessing if the message doesn't match this exact format — an unparsed reset time means `run_recovery_check` will attempt on every invocation instead of waiting, which is safe (worst case: an extra no-op API call attempt) but not silent.
- `run_recovery_check(...)` — the idempotent check-and-run entrypoint, meant to be invoked repeatedly (see Scheduling below). Before the reset time: no-op, prints time remaining, **never calls the live re-run function** (verified by test — this matters because calling it early would just waste another quota-exceeded attempt). After the reset time: calls the injected `live_rerun_fn`, and on success writes a before/after markdown report to `outputs/eval_reports/` and marks state `completed`. If the live call fails again with a *new* parseable quota message (e.g., a different job later hit its own limit), the reset time is updated and state stays `pending` — this is not treated as failure. If it fails with anything else (auth error, code bug, network issue that outlasts the scheduling interval), state moves to `failed` and the CLI exits non-zero, so a scheduler's failure notification actually means something.
- `generate_comparison_report(target, before, after)` — a plain markdown table, one row per metric key present in either snapshot, with changed values flagged inline. Deliberately dumb/generic (no hardcoded metric names) so it works for whatever fields a future `before`/`after` snapshot happens to contain.
- The live re-run function itself (`_live_rerun` in `evaluation_recovery.py`) is injected into `run_recovery_check` rather than imported directly, specifically so the state machine is unit-testable without any real API access — `tests/test_eval_recovery.py` exercises every branch (not-ready, succeeds, still-blocked, unexpected-failure, already-completed, already-failed) with a fake `live_rerun_fn`.

**Scheduling instructions.** `check-and-run` is safe to invoke on a fixed interval — cheap when there's nothing to do (one JSON read, no network call), and self-terminating once `completed`. Two options:

1. **Cron (portable, no dependency on this session or Claude Code running):**
   ```cron
   # Check hourly whether the quota has reset and re-run the pending evaluation
   0 * * * * cd /path/to/job-search-agent && /usr/bin/python3 evaluation_recovery.py check-and-run >> /tmp/eval_recovery.log 2>&1
   ```
   Add with `crontab -e`, replacing `/path/to/job-search-agent` with this repo's actual location. Logs to `/tmp/eval_recovery.log`; check `python3 evaluation_recovery.py status` any time for the current state without waiting for the next cron tick.

2. **Claude Code's own scheduler** (the `schedule` skill / `CronCreate` tool) — schedules a cloud-run invocation instead of relying on a local crontab surviving until the reset time. This is the more "automatic" option but sets up real recurring/scheduled infrastructure, so it's something to set up deliberately with the user rather than something a coding assistant should create silently on the user's behalf.

Either way, the underlying check is the same idempotent command — the scheduling mechanism is just what calls it.

### Pipeline timing instrumentation

**Why this was added.** The pipeline had no way to answer "where is time actually being lost?" — only anecdote. Prior session notes recorded a concrete instance: two high-scoring jobs were lost to what the notes call "shortlist-sitting" — they cleared the fit-score bar but sat un-applied-to until the postings closed. The stated rule ("80+ scores get applications within 48h") existed as a policy with no instrumentation checking whether it was actually being followed.

**Design — three timestamps, set at the exact point each transition already happens in the code:**

| Field | Set in | When |
|---|---|---|
| `shortlisted_at` | `module2_scoring.score_all_discovered()` | The moment a job's fit score first clears `MIN_FIT_SCORE` |
| `application_submitted_at` | `module5_apply.apply_to_shortlisted()` | The moment the human confirms they submitted the application |
| `closed_or_expired_at` | `module6_tracker.update_status()` | The moment a job's status is set to `closed` |

All three are additive (`job.setdefault(...)` or plain assignment alongside the existing status/date fields already being set there) — no existing field is renamed, removed, or repurposed. `applied_date` (a bare date, pre-existing) and `last_updated` are untouched; `application_submitted_at` is a new, separate, full-timestamp field alongside `applied_date`, not a replacement for it.

**The graceful-degradation decision.** These timestamps only exist going forward from when this instrumentation shipped — none of the 294 jobs already in `data/job_queue.json` were shortlisted or applied to *with a timestamp recorded*, and there's no way to reconstruct one without fabricating data. Rather than wait for new data to accumulate before this becomes useful, `modules/lifecycle_metrics.compute_metrics()` splits its metrics into two kinds:

- **Timestamp-dependent** (average/median shortlist-to-apply hours, "applied late"): require both `shortlisted_at` and `application_submitted_at` to be present and parseable. Zero historical jobs have these, so these read as "no data yet" today — expected, not a bug — and will populate as jobs move through the pipeline from here on.
- **Status-dependent** (never-applied, closed-before-application, "still pending and delayed"): computed from `status` and `fit_score` alone, which every job already has. These are meaningful *immediately*, including against the existing 294-job queue — running the report today already surfaces real incidents that motivated this work (several high-scoring jobs closed before ever being applied to).

**What decisions this enables.** The report's "high-score jobs delayed" and "closed before application" sections turn "we lost some good jobs to slowness" from an anecdote into a specific, named, sorted-by-score list — the actionable answer to "which job should I apply to right now" and "which pattern of loss should I fix in the process." Over time, the average/median duration numbers answer whether the 48-hour rule is actually being met, not just stated.

**Report.** `pipeline_timing_report.py` writes a markdown report to `outputs/reports/` (gitignored, like the rest of `outputs/`) and prints the same content to stdout. `modules/lifecycle_metrics.generate_report()` is a pure function over `compute_metrics()`'s output, so the report content is independently testable from the file-writing/CLI concerns.

**Verification.** `tests/test_lifecycle_metrics.py` (35 tests) covers: timestamp math including fractional hours, `Z`-suffixed and naive (no-timezone) timestamps, and malformed/missing values (never raises, always returns `None`); average/median over known values including the even-count case; every status-dependent metric independent of timestamp presence (proving the graceful-degradation design actually degrades gracefully); the priority/sorting behavior (results ordered by fit score, highest first); and missing-data scenarios (empty job list, a job missing every field, a mixed batch of complete and incomplete jobs). Each of the three write points (`module2_scoring`, `module5_apply`, `module6_tracker`) was also verified directly against an isolated temp queue file, confirming the actual instrumentation — not just the metrics math — works end-to-end.

### Performance baseline instrumentation

**Why this was added.** Prompt caching (§ "Retry policy" background and `docs/EVALUATION_REPORT.md` § 6) had been identified as the single largest remaining cost lever, but there was no way to measure its actual impact — no per-module cost breakdown, and no timing data at the pipeline-stage level (only per-LLM-call latency, from the existing `tracked_create`/`tracked_create_async` instrumentation). Implementing an optimization without first measuring what it's optimizing away from means the "after" number has nothing real to compare against.

**Design — two additions, both purely observational:**

- **`modules/util.track_stage()`**: a context manager measuring wall-clock time for one pipeline-stage unit of work (e.g. scoring one job, tailoring one resume), logged to `data/pipeline_stage_log.jsonl`. Wired into the per-job function in every LLM-driving module (`module1_discovery.discover_jobs`, `module2_scoring.score_job`, `module2b_board_review.run_advisory_board`, `module3_resume.tailor_resume`, `module3b_resume_board.review_resume`, `module4_coverletter.generate_cover_letter`). Deliberately *not* wired into `module5_apply` (its timing includes human decision time, not agent performance) or `module6_tracker` (no LLM calls). Like `tracked_create`, it never affects the wrapped code's return value and re-raises whatever the wrapped block raises, after logging that it failed — a timing side-channel must never become a second way for the pipeline's actual behavior to change.
- **`modules/performance_baseline.py`**: pure aggregation functions over both existing logs — `compute_api_usage_by_module()` rolls up `tracked_create`/`tracked_create_async`'s per-call log by the module that issued the call (calls, tokens, cost); `compute_stage_timing_by_module()` and `slowest_modules()` do the same for `track_stage`'s per-stage log (run count, average/median/total duration, ranked slowest-first). `performance_baseline_report.py` is the CLI, matching the existing `usage_report.py`/`pipeline_timing_report.py` pattern.

**The contaminated-log decision.** While building this, `data/llm_usage_log.jsonl` (gitignored, not source-of-truth data) was found to contain 40 entries from earlier development-time verification — mocked Anthropic responses used to test `tracked_create` itself, plus the one real quota-exceeded failure from the live date-grounding verification attempt (§ "Evaluation recovery workflow"). None of it represents real measured API cost. It was backed up and reset before writing `docs/PERFORMANCE_BASELINE.md`, so that document's "current measured data" honestly reads zero rather than presenting test artifacts as if they were a real baseline.

**The structural-estimate decision.** With the account still over its usage quota (§ "Evaluation recovery workflow"), no live calls — not even token counting — were available to establish real numbers. Rather than wait, `docs/PERFORMANCE_BASELINE.md`'s cost analysis uses each module's actual prompt-construction code run against real data already in this repo (a real job, a real tailored resume, the real master resume), with token counts approximated via a chars÷4 heuristic. This is clearly labeled as a rough, reproducible-right-now estimate, not a substitute for the real measured numbers `usage_report.py`/`performance_baseline_report.py` will produce on the next live run — the two are not meant to be confused with each other.

**A real, previously-undocumented finding this surfaced.** Computing the structural estimate for `module2b_board_review` revealed that `run_advisory_board()`'s `json.dumps(job, indent=2)` serializes the *entire* job dict into every reviewer's prompt — for a freshly-scored job that's ~1,900 tokens, but for a job that has already accumulated `board_reviews`/`resume_board`/`resume_scorecard` data, the same call balloons to ~11,600 tokens for the identical reviewing task. This wasn't caught by any prior review because no one had computed the actual prompt size against real accumulated job records before. See `docs/PERFORMANCE_BASELINE.md` § 3–5 for the full analysis; this document does not fix it (out of scope — instrumentation and measurement only, no behavior change).

**Verification.** `tests/test_performance_baseline.py` (25 tests) covers: `track_stage`'s success/failure logging and exception re-raising, that extra keyword arguments are merged into the logged entry, that multiple calls append rather than overwrite; `module_for_label`'s mapping for every real label prefix in the codebase plus unrecognized-label and empty-string cases; `compute_api_usage_by_module` and `compute_stage_timing_by_module`'s aggregation correctness including failed-call handling and missing-field defaults; and `slowest_modules`' sort order including a stage with no recorded average. Each of the six instrumented modules was also verified directly (mocked Anthropic calls, both sync and async, both success and failure paths) to confirm the actual wiring — not just the underlying context manager — logs correctly, with zero real API calls made anywhere in this verification.

### Module 2b prompt context: bounding the job-JSON dump

**Where this was.** `modules/module2b_board_review.py`'s `run_advisory_board()` built every reviewer's prompt as `content = f"...\n\nJOB:\n{json.dumps(job, indent=2)}\n\nRESUME:\n{resume}"` — serializing the *entire* job dict, not just its posting content. All four reviewers (`asyncio.gather`) received this same string.

**Why this blocked caching.** Anthropic's prompt cache is a byte-identical prefix match. The full `job` dict carries the pipeline's *accumulated history* on top of the posting itself — `fit_score`, `fit_reasons`, `fit_gaps`, `status`, `shortlisted_at`, and, on any re-run, `board_reviews`, `board_decision`, `resume_board`, `resume_scorecard`, `resume_v2_path`, `applied_date`/`application_submitted_at`/`closed_or_expired_at`, `notes`. That content mutates as a job moves through later stages — some of it (`board_reviews`, `board_decision`) is literally *this call's own output* from a prior run. Two consequences, independent of whether `cache_control` is ever added:

1. **The same job's content isn't stable over time.** A re-run of board review against a job that has since picked up downstream fields produces different bytes than the original call did — so even the narrowest possible cache win (an identical call repeated on the identical job) was structurally impossible.
2. **Payload size was unbounded and unpredictable**, growing with however much history a given job happened to carry — see the finding recorded under "Performance baseline instrumentation" above (~1,900 tokens fresh vs. ~11,600 tokens once a job had accumulated review data, a 6x spread for the same reviewing task).

A secondary issue: `content` was one flattened f-string, not separate content blocks. Even setting the job-blob problem aside, there was no seam to attach a `cache_control` breakpoint to just the resume or instructions portion without restructuring the message first.

**Smallest change made.** Added `POSTING_FIELDS = ("title", "company", "location", "url", "description")` and `_posting_context(job)`, which extracts only those fields, defaulting any that are missing to `""` so every job serializes to the same shape regardless of how far it's progressed through the pipeline. `run_advisory_board()` now calls `json.dumps(_posting_context(job), indent=2)` instead of `json.dumps(job, indent=2)` — one line. Nothing else changed: the resume text, the four reviewer prompts, the chair synthesis, the parsed output shape, and the job-mutation logic (`_apply_board_decision`) are untouched.

**Measured impact** (real data from `data/job_queue.json`, run through the actual `_posting_context()` code — see `docs/PERFORMANCE_BASELINE.md` § 3 for the full table):

- **Typical case** (a job about to be board-reviewed for the first time, no accumulated history yet — 10 such jobs currently queued): avg. dump size drops from 6,765 to 5,546 characters (~18%). Modest, because a freshly-shortlisted job doesn't carry much beyond the posting yet — the description text dominates either way.
- **Re-processing / worst-case exposure** (45 jobs that have already been through board review, measured against their current, further-progressed state — i.e. what re-running this call against them today would have cost): avg. dump size drops from 48,662 to 3,553 characters (**92.7%** reduction). One job in this set went from 46,348 to 284 characters. This is the unbounded-growth exposure the fix eliminates — cost is now capped by the posting content alone, not by however much pipeline history a job has accumulated.

**Expected effect on future caching.** This removes one of the two blockers `docs/PERFORMANCE_BASELINE.md` § 6 identified for this call site — the per-job-varying, growing blob — but does not itself add caching (no `cache_control` was touched here). What it buys for later work: the same job's content is now guaranteed byte-identical across repeat calls (a necessary precondition for any cache hit to ever be possible here), the payload is bounded and small, and `content` is now built from two clearly-separated pieces (a fixed-shape posting dict + the resume) instead of an opaque whole-object dump — a much easier starting point for splitting the message into cacheable/non-cacheable blocks later. The *other* blocker is unaffected: the four reviewers still fan out via `asyncio.gather`, and a cache entry only becomes readable once the request that wrote it begins streaming, so they still can't cache off each other within a single job's review. That remains a known limitation for whenever caching itself is implemented.

**Verification.** `tests/test_module2b_board_review.py` (8 tests) covers: `_posting_context()` extracting only the five posting fields and excluding every pipeline-history field tested (`fit_score`, `board_reviews`, `resume_scorecard`, `status`, etc.); missing fields defaulting to `""` rather than raising; identical output shape for a fresh job vs. one carrying full accumulated history (the core property this fix relies on); and, against a mocked `tracked_create_async` with zero real API calls, that `run_advisory_board()`'s actual reviewer prompts exclude history fields, still contain the posting fields and resume text, are byte-identical across all four reviewers, stay byte-identical when the same job is re-run after accumulating history, and that `run_advisory_board()`'s return shape and values are unaffected.

### Prompt caching (module2_scoring, module3_resume, module4_coverletter)

**Why these three first.** `docs/PERFORMANCE_BASELINE.md` § 6 identified the three sequential, single-call-per-job stages as the highest-confidence caching target: each loops over up to 294 jobs, and each call sends the *same* stable content (candidate profile and/or master resume) alongside job-specific content that changes every time. The parallel fan-out reviewers (module2b, module3b) are excluded from this round on purpose — they dispatch via `asyncio.gather`, and a cache entry only becomes readable once the request that wrote it begins streaming, so concurrent calls can't read a cache another is still writing. That limitation is unrelated to what's fixed here and still applies.

**Design.** Anthropic's prompt cache is a byte-identical prefix match (`tools` → `system` → `messages`, cumulative). In all three modules, the content that's identical on every call was already either fully before the job-specific content (module2, module4) or fully after it (module3). The fix in each case:

- Split the single `messages[0].content` string into two text blocks: a stable block (candidate profile and/or resume) and a volatile block (job title/company/description + the instruction), instead of one flat f-string.
- `cache_control: {"type": "ephemeral"}` on the stable block only. `system` (already a separate, per-module-fixed field) is left as a plain string — it doesn't need its own breakpoint, because the messages-tier breakpoint's cache key already covers everything before it cumulatively, including `system`.
- **`module3_resume` required reordering** (master resume moved from after the job posting to before it) — the only prompt-content change in this round, made because caching requires the stable content to be a *prefix*, not a suffix. All the same information is still sent, in the same wording, with the final instruction ("Rewrite the resume to best match this job.") still the last thing in the prompt. `module2_scoring` and `module4_coverletter` needed no reordering — verified by `tests/test_prompt_caching.py` to be byte-identical to the pre-change prompt after concatenating the two blocks back together.

**The honest limitation: this doesn't produce a cache hit today.** Anthropic enforces a minimum cacheable prefix length per model — shorter prefixes silently don't cache (no error, `cache_creation_input_tokens: 0`). `CLAUDE_MODEL` (`claude-sonnet-4-6`) requires **2048 tokens**. Measured against the real content in this repo (chars÷4 estimate, same methodology as `docs/PERFORMANCE_BASELINE.md` § 1):

| Module | Stable content | Estimated tokens |
|---|---|---:|
| `module2_scoring` | profile + resume excerpt | ~841 |
| `module4_coverletter` | profile + resume excerpt | ~592 |
| `module3_resume` | system prompt + full master resume | ~1,665 |

All three fall short of the 2048-token minimum — `module3_resume` closest, still ~383 tokens short. This is a genuine content-size constraint, not an implementation bug: the minimum exists so the ~1.25x cache-write premium is only paid where there's enough repeated content to amortize it against. No content was padded to force it past the threshold — that would waste real tokens on every call for a benefit that isn't guaranteed to materialize, and would contradict the goal of reducing token cost. The architecture is correct and complete; it activates automatically, with no further code changes, if the combined stable content grows past 2048 tokens (a longer resume, a longer candidate profile) or `CLAUDE_MODEL` changes to something with a lower minimum (Sonnet 4.5/4.1/4/3.7 need only 1024). See `docs/PERFORMANCE_BASELINE.md` § 6 for the full before/after and the projected-versus-measured savings distinction.

**Instrumentation extended to see it when it does activate.** `modules/performance_baseline.py`'s `compute_api_usage_by_module()` now also sums `cache_creation_input_tokens`/`cache_read_input_tokens` per module (these fields were already logged by `tracked_create`/`tracked_create_async`, just not rolled up), and `performance_baseline_report.py`'s table gained `cache_wr`/`cache_rd` columns. The day real traffic clears the minimum, this shows up in the existing report with no further code changes.

**A near-miss caught during this work.** The new tests initially called `score_job`/`tailor_resume`/`generate_cover_letter`/`run_advisory_board` for real (only `tracked_create`/`tracked_create_async` were mocked), which meant the real, unmocked `track_stage()` calls inside them wrote 50 test-artifact entries into the actual `data/pipeline_stage_log.jsonl` — the same class of contamination `docs/PERFORMANCE_BASELINE.md` had already found and backed out of `llm_usage_log.jsonl` once. Caught before being reported as real data: the log was backed up and reset, and both new test files (`tests/test_prompt_caching.py`, `tests/test_module2b_board_review.py`) now redirect `util.PIPELINE_STAGE_LOG_PATH` to a tempfile in `setUp`/`tearDown`, matching the isolation pattern `tests/test_performance_baseline.py` already used.

**Verification.** `tests/test_prompt_caching.py` (13 tests) covers, per module: the message content is exactly two blocks with `cache_control` on the first only; for `module2_scoring`/`module4_coverletter`, the two blocks concatenated are byte-identical to the pre-change single-string prompt; for `module3_resume`, every original fragment (master resume, job title/company/description, the closing instruction) is still present post-reorder and the instruction still ends the prompt; `system` and other `messages.create()` arguments are unaffected; and each function's return value is unaffected by the restructuring. `tests/test_performance_baseline.py` gained 2 tests confirming `compute_api_usage_by_module()` correctly aggregates `cache_creation_input_tokens`/`cache_read_input_tokens` (and defaults missing ones to zero for pre-caching log entries). 132 tests total, all passing, zero real API calls.
