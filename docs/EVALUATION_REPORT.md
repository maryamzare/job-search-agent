# AI Agent Evaluation Report

**Repository:** [github.com/maryamzare/job-search-agent](https://github.com/maryamzare/job-search-agent)
**Scope:** A structured evaluation of this pipeline's real output and architecture, and the engineering work that followed from it.
**See also:** [`ARCHITECTURE.md`](../ARCHITECTURE.md) for the full technical detail behind each design decision summarized in § 4, and [`CHANGELOG.md`](../CHANGELOG.md) for the complete change history in the order it happened.

---

## 1. Executive Summary

### What the job-search agent does

This repository is an AI-powered pipeline that automates a technical job search end to end, built on the Claude API:

```
discover → score → board review → tailor resume → resume board → cover letter → apply → track
```

Eight stages, each an owned module (`modules/module1_discovery.py` through `module6_tracker.py`), orchestrated by `main.py` and backed by a single JSON state store (`data/job_queue.json`). It is a working tool, not a demo — it has processed 294 real job postings, generating real tailored resumes, real cover letters, and tracking 33 real applications through to interviewing/offer/rejected outcomes.

### Why it was evaluated

A pipeline like this either works because someone checked, or it "works" because nothing has visibly broken yet. This project had reached the second state: real output existed (scores, board verdicts, tailored resumes, cover letters) but nothing had cross-checked that output against the data already sitting in `data/job_queue.json`, and there was no reliability layer at all around the Claude API calls driving every stage. The evaluation that produced this report was deliberately grounded in that real, existing data — sampled resumes checked against the master resume for fabricated claims, stored board scorecards averaged and inspected for patterns, the real application funnel counted directly — rather than assumption or a fresh round of live API calls.

### Major improvements made

The evaluation surfaced concrete, previously invisible defects, and the engineering work that followed fixed them and added the reliability/observability layer the pipeline lacked entirely:

- A **systemic hallucination** causing false "future-dated employment" flags on ~95% of resume-board reviews, triggering unnecessary rewrites.
- An **advisory board whose verdict the pipeline silently ignored** — jobs the board said to skip still got resumes and cover letters drafted for them.
- An **apply step pointing at the wrong resume version**, undermining the entire resume-board feature.
- A **retry bug that retried an unrecoverable error five times** before failing anyway, later replaced with a policy that correctly distinguishes retryable rate limits from unrecoverable quota exhaustion.
- **Zero reliability or observability infrastructure** — no retry policy, no cost/latency tracking, no automated tests, no lifecycle timing data — replaced with a retry policy, an automated evaluation-recovery workflow, 79 automated tests, and pipeline timing instrumentation that surfaced real, previously-anecdotal application losses on its first run.

---

## 2. Before vs. After Metrics

| Area | Before | After |
|---|---|---|
| **Reliability — retry classification** | 1 undifferentiated bucket: any `anthropic.APIStatusError` (400/401/403/404/429/5xx) was retried up to 3× with exponential backoff, including a real quota-exceeded 400 that was retried 5 times across parallel reviewer calls before failing anyway | 4-way classification (`rate_limited` / `quota_exceeded` / `transient` / `non_retryable`), each handled correctly — see § Retry behavior below |
| **Retry behavior — 429 rate limits** | Not distinguished from quota exhaustion; an intermediate fix excluded 429 from retry entirely | Retried with exponential backoff; honors a `Retry-After` header when present (capped at 60s against a malicious/broken value), falls back to backoff otherwise |
| **Retry behavior — quota exhaustion** | Retried 5× with exponential backoff (wasted latency on a failure that cannot resolve for hours to days) | Fails immediately (0 retries, ~instant), prints a clear notification, re-raises the original exception unmodified so a caller can persist recovery state |
| **Evaluation recovery** | None — a quota-blocked live verification just failed; resuming it required a human to remember and manually re-run it later | Automated `pending → completed \| failed` state machine persisted to disk; `evaluation_recovery.py check-and-run` is safe to schedule (cron or otherwise), no-ops cheaply before the reset time, and auto-runs the deferred evaluation once the account's quota resets |
| **Testing coverage** | 0 automated tests anywhere in the project | 79 tests across 3 files (`test_retry.py`: 30, `test_eval_recovery.py`: 14, `test_lifecycle_metrics.py`: 35), all passing, all runnable with no API key or network access |
| **Pipeline observability — cost/latency** | No visibility into what any Claude call cost or how long it took | Every call logged (latency, tokens, estimated cost) to `data/llm_usage_log.jsonl` via `tracked_create`/`tracked_create_async`; `usage_report.py` summarizes per call type |
| **Pipeline observability — lifecycle timing** | "Jobs lost to shortlist-sitting" was anecdote (recorded in prior session notes, never measured) | Three new lifecycle timestamps + `pipeline_timing_report.py`; the status-based metrics alone (no new data needed) already surfaced real high-scoring jobs closed before ever being applied to, on the very first run against the live 294-job queue |

---

## 3. AI Agent Architecture Evaluation

**Prompt strategy.** The pipeline uses a specialist-reviewers-plus-synthesizer pattern for both quality gates: module 2b runs 4 reviewers (fit, strategy, risk, effort) plus a chair; module 3b runs 5 reviewers (ATS, impact, relevance, narrative, red-flags) plus a chief editor. Each reviewer stays scoped to one question rather than diluting a single do-everything prompt. The evaluation found one real prompt defect (duplicated, drifting candidate-profile blocks — see § Engineering Decisions) and one systemic reasoning gap (no date-grounding); it also explicitly audited the reviewer prompts themselves and found them already well-scoped, so they were deliberately left unchanged rather than edited for its own sake on a system driving real, live application content.

**Tool usage.** Six Anthropic client instantiations were independently duplicated across modules (now centralized behind `modules/util.get_client()`/`get_async_client()`); one client (`sync_client` in module 3b) was fully dead — constructed, never called, removed. Scraping tools (`requests` + BeautifulSoup against LinkedIn/Greenhouse/Lever) were reviewed and found fragile against real anti-bot behavior (documented in prior session notes: several major career sites present real scraping obstacles — bot detection, JS-rendered pages, no public API) but were left unmodified — fixing external-site scraping reliability is a different, higher-effort, higher-risk problem than the internal reliability work in scope here.

**Failure handling.** This is where the most iteration happened. The original retry logic treated every non-2xx response as equally retryable. A first fix narrowed that to transient failures only, but overcorrected by excluding genuine rate limits. The current design (§ 2, § 4) classifies by both exception type and message content, with quota-exhaustion detection checked first so it can't be misclassified even if a provider ever reports it under an unexpected status code.

**Data flow.** `data/job_queue.json` is the single source of truth for every job's lifecycle. Two real data-flow defects were found and fixed: the board's verdict never actually gated the pipeline (resumes/cover letters were generated regardless of a "skip" decision), and the apply step read the pre-board-review resume draft even after a better, board-reviewed version existed. Both are now wired correctly. Writes are atomic (temp file + rename) and incremental (saved after each item in a batch, not once at the end) — a fix that directly followed from a real incident where a mid-run failure lost an entire batch's progress.

**Evaluation methodology.** Grounded in real, already-existing data rather than fresh live calls wherever possible: resume-board scorecards already stored in `data/job_queue.json` were aggregated and inspected for patterns (this is how the 95%-false-positive date bug was found); sampled tailored resumes and cover letters were cross-checked against `data/master_resume.txt` for fabricated claims (none found in the sampled set); the real application funnel was counted directly from stored statuses. Live API calls were used sparingly and deliberately — gated behind explicit confirmation given real account usage limits, and one live verification attempt is still pending the account's quota reset (see § 5).

---

## 4. Engineering Decisions

For each major change: the problem discovered, the decision made, the tradeoff considered, and the result.

### Board decision never gated the pipeline

- **Problem discovered:** `module2b_board_review.py` computed an apply/defer/skip verdict, but nothing downstream read it — `main.py`'s resume/cover-letter steps ran on any job with status `shortlisted` or `board_approved`, and since the board never produced `board_approved`, a job the board said to skip stayed `shortlisted` and still got materials drafted.
- **Decision:** wire the verdict into an actual status transition (`apply` → `board_approved`, `skip` → `filtered_out`, `defer` → left unchanged for a human call), and update the apply step to recognize `board_approved`.
- **Tradeoff considered:** 45 jobs already carried a stored `board_decision` from before this fix, 12 of them sitting at `shortlisted` with a `defer` verdict. Retroactively migrating those would touch real, human-curated application state. Chose a forward-only fix instead, and flagged the 12 stale records for optional manual reconciliation rather than silently rewriting them.
- **Result:** the board's verdict now has real effect; the apply step surfaces jobs it previously missed entirely.

### Apply step pointed at the wrong resume

- **Problem discovered:** `module5_apply.py` always resolved the pre-board-review v1 resume path, even for jobs that had already been through the 5-reviewer resume board and had a corrected v2 on disk.
- **Decision:** prefer `resume_v2_path` when it exists, fall back to v1 otherwise; label the displayed path `(board-reviewed)` when v2 is used.
- **Tradeoff considered:** none significant — purely additive, backward-compatible for the many jobs that never went through the resume board at all.
- **Result:** verified against real data — a sampled job resolved correctly to its `_v2.txt` file with the board-reviewed label shown.

### Reviewers had no notion of the current date

- **Problem discovered:** aggregating stored resume-board scorecards showed 39 of 41 reviewed resumes carrying a "major-flags" verdict, all citing the same root cause: the red-flag reviewer reading a real, already-past employment end date as suspiciously future-dated, because nothing in the prompt ever told the model what day it actually was.
- **Decision:** prepend a one-line `current_date_context()` to the shared reviewer context in the two modules that do this kind of qualitative, date-sensitive reasoning (module 2b, module 3b).
- **Tradeoff considered:** did not extend this to the scoring/resume-tailoring/cover-letter modules, since there was no evidence they reason about dates the same way — avoided an unnecessary prompt change on live-facing content without a demonstrated defect to fix.
- **Result:** verified via mocked Anthropic calls that the exact prompt text now starts with the real current date. Whether this changes the reviewer's actual verdict is still unverified live (see § 5) — the account's usage quota is exhausted until a scheduled reset.

### Retry policy — two iterations

- **Problem discovered:** the retry wrapper caught `anthropic.APIStatusError`, the base class for every non-2xx response, so a real quota-exceeded error (HTTP 400) was retried 5 times with exponential backoff before failing anyway.
- **Decision (iteration 1):** narrow retryability to a strict allowlist — network errors, timeouts, HTTP 500/502/503/504 only.
- **Tradeoff considered (iteration 1):** this also excluded HTTP 429 (genuine rate limits) entirely, on the reasoning that a fix should close the one verified incident without broadening scope.
- **Decision (iteration 2, on review):** split into four categories — `rate_limited` (429, retries with backoff and honors `Retry-After`), `quota_exceeded` (message-content detected, fails fast, notifies), `transient` (network/5xx, retries), `non_retryable` (auth/permission/other 4xx, fails fast) — with quota-exhaustion message content checked before status code, so it classifies correctly even under an unexpected status.
- **Tradeoff considered (iteration 2):** deliberately did not have the retry utility call directly into the evaluation-recovery module's state-saving function — `modules/util.py` is a dependency-free leaf module that the recovery module already imports from, so the reverse import would be circular, and the retry utility has no per-call "what to recover" context to give it anyway.
- **Result:** 30 tests proving every category's exact behavior, including the priority-ordering edge case. Zero wasted retries on unrecoverable errors; correct backoff-and-`Retry-After` handling on genuine rate limits.

### Evaluation recovery workflow

- **Problem discovered:** live verification of the date-grounding fix hit real account quota exhaustion mid-session, with no way to resume automatically once the quota reset.
- **Decision:** persist the failed attempt (what to re-check, a "before" snapshot, and the reset time parsed out of Anthropic's own error message) to a gitignored state file; provide an idempotent `check-and-run` CLI command safe to invoke on a schedule.
- **Tradeoff considered:** chose not to fold this into ordinary pipeline runs — the queue's own incremental-save fix (below) already gives normal runs equivalent resilience, so a second, overlapping state store for the common case would be redundant.
- **Result:** real recovery state seeded for the actual blocked job; verified the CLI correctly reports "not ready" and takes no action (no wasted API call) before the reset time arrives.

### Batch operations saved progress once, at the end

- **Problem discovered:** `score_all_discovered()`, `review_shortlisted()`, and `review_resumes()` each looped over every eligible job, then wrote `job_queue.json` once after the whole loop — a crash partway through lost every already-completed job in that run, a failure mode already observed in practice (a missing API key mid-run).
- **Decision:** save after each item instead of once at the end; also switch to atomic writes (temp file + rename) so a crash mid-write can't truncate the file.
- **Tradeoff considered:** more frequent disk writes against a file that's currently small (~25K lines) — negligible next to the LLM call latency each iteration already pays.
- **Result:** a crash mid-run now costs at most one in-flight job instead of the whole batch.

### Pipeline lifecycle timing instrumentation

- **Problem discovered:** "high-scoring jobs lost to slow follow-up" existed only as anecdote in prior session notes, with nothing measuring whether it was still happening.
- **Decision:** add three lifecycle timestamps at the exact points those transitions already happen in the code; split the resulting metrics into status-based (usable immediately, no new data needed) and timestamp-based (needs new data to accumulate).
- **Tradeoff considered:** explicitly declined to backfill timestamps for the 294 existing jobs — there was no way to know when they were actually shortlisted or applied to without fabricating it, so the duration metrics honestly read "no data yet" today rather than showing a plausible-looking but invented number.
- **Result:** the status-based metrics alone — no new data required — surfaced real, concrete high-scoring jobs closed before ever being applied to, on the first report run against the live queue.

### Public-repo history sanitization

- **Problem discovered:** two commit messages named the actual employer and role names the candidate was applying to — fine for a private working session, not appropriate to publish permanently in a public portfolio repository's history.
- **Decision:** reword both messages non-interactively (branch, amend, cherry-pick — no interactive rebase), verifying the resulting file-content tree was byte-identical to the original before repointing `main`.
- **Tradeoff considered:** none — both commits were still local and unpushed, so this carried no shared-history risk.
- **Result:** clean history pushed to GitHub with zero specific-employer references, zero content drift.

---

## 5. Current Limitations

- **Missing job descriptions.** 111 of 294 jobs (38%) have an empty `description` field — company research/JD capture is incomplete for over a third of the pipeline, traceable to real scraping fragility against sites without a public API or with active anti-bot measures. This wasn't addressed in this round of work; it's a real, open gap.
- **Need for more historical timing data.** Zero of the 294 existing jobs carry the new `shortlisted_at`/`application_submitted_at` timestamps — they were added this round and are forward-only by design (backfilling would mean fabricating data that doesn't exist). The average/median shortlist-to-apply duration metrics will only become meaningful once new jobs accumulate real timestamps going forward.
- **API cost optimization opportunities.** No prompt caching exists anywhere in the pipeline, despite the full master resume and every system prompt being resent in full on every one of roughly a dozen distinct call types, across every job the pipeline processes. `usage_report.py` now exists to measure this, but has no real data yet since no live pipeline run has occurred since it was added.
- **One fix's live verification is still pending.** The date-grounding fix (§ 4) is verified structurally (the correct date is now sent) but not behaviorally (whether the reviewer's actual verdict changes) — blocked on the account's usage quota, with automated recovery already in place (§ 2) to close this out once the quota resets.
- **Retry/backoff coverage is uneven.** Only the resume-board module's async Claude calls go through the retry policy in § 4. The scoring, resume-tailoring, and cover-letter modules' synchronous calls have no retry/backoff at all — a transient network blip there still fails the whole batch.

---

## 6. Future Roadmap

- **Prompt caching.** The single largest remaining cost/latency lever: the master resume and system prompts are resent uncached on every call across every job. `usage_report.py` is already in place to produce a real before/after comparison once this is built.
- **Better company research.** Close the 38%-missing-description gap identified in § 5 — retry logic for failed scrape attempts, and alternate data sources for the sites that are hardest to scrape reliably today.
- **ATS optimization experiments.** Once prompt caching makes iteration cheap, A/B test reviewer prompt wording against real `ats_score` outcomes already being collected in `data/job_queue.json`, rather than guessing at wording improvements.
- **Notification system for high-priority jobs.** `pipeline_timing_report.py` already computes which high-scoring jobs are sitting past the delay threshold; the natural next step is pushing that alert proactively (email, Slack, or similar) instead of requiring a human to remember to run the report.

Additional, lower-priority items noted along the way: extend retry/backoff coverage to the scoring/resume/cover-letter modules' synchronous calls (§ 5), and complete the pending live verification of the date-grounding fix once the account's usage quota resets.
