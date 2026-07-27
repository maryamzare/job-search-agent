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

Reviewer prompts that reason qualitatively about a resume (red flags, career-arc coherence) have no inherent notion of "today" — an LLM call has no wall-clock access unless told. `modules/util.current_date_context()` is prepended to the shared reviewer context in module2b and module3b for exactly this reason: without it, a real past date can be misread as suspiciously future-dated (this was found happening on ~95% of resume-board reviews in production data, each one incorrectly flagging the candidate's real, already-past INRIX end date as a red flag and triggering an unnecessary rewrite).

### Retry policy: only retry what retrying can fix

**Problem.** The original retry wrapper (`module3b_resume_board._with_retry`) caught `anthropic.APIStatusError`, the base class for *every* non-2xx HTTP response — 400 (invalid request / quota exceeded), 401 (auth failure), 403, 404, and 429 alongside the 500-series errors it was actually meant to protect against. In practice, a quota-exceeded response (which Anthropic returns as an `invalid_request_error` with HTTP 400) was retried 5 times with exponential backoff before failing anyway — wasted latency on a failure mode that retrying can never resolve, since the account-level condition doesn't change between attempts a few seconds apart.

**Fix.** `modules/util.py` now exposes `is_transient_error()` and `with_retry()`, and every retry call site in the codebase goes through them. The predicate is a strict allowlist, not a blocklist — anything not explicitly recognized as transient is treated as non-retryable:

- **Retry:** `anthropic.APIConnectionError` (network failures) and its subclass `anthropic.APITimeoutError` (request timeouts); HTTP 500, 502, 503, 504 (transient server-side failures).
- **Never retry:** everything else — 400 (invalid request / quota exceeded), 401 (auth failure), 403, 404, and any other 4xx. These are outcomes retrying cannot change; the request fails again on identical terms, so retrying only adds latency with no chance of success.

**Deliberate omission — HTTP 429 (rate limit).** 429 is commonly treated as retryable elsewhere (Anthropic's own SDK auto-retries it by default), but it is intentionally left out of `TRANSIENT_HTTP_STATUS_CODES` here. This is a judgment call, not an oversight: the policy was written to close a specific, verified production incident (a 400 quota-exceeded error retried needlessly), and extending it to also cover 429 would broaden the change beyond what's been observed and tested. If a real 429 is ever seen failing a call that a short backoff would have recovered, add `429` to `TRANSIENT_HTTP_STATUS_CODES` in `modules/util.py` — the mechanism already supports it; it's a one-line change.

**Verification.** `tests/test_retry.py` proves: the predicate correctly classifies every status code discussed above (transient vs. not); a transient error retries and can succeed within the retry budget; a transient error that never succeeds exhausts its retries and raises; backoff delay doubles on each attempt; and a non-transient error fails on the very first attempt with zero retries and zero backoff sleep.

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
