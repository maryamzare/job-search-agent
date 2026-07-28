# Performance Baseline

**Purpose:** establish a measured cost/latency/token baseline *before* implementing prompt caching, so the improvement can be quantified against something real rather than asserted. This document does not implement caching — see [`docs/EVALUATION_REPORT.md`](EVALUATION_REPORT.md) § Future Roadmap and [`ARCHITECTURE.md`](../ARCHITECTURE.md) for that plan.

**A note on the numbers below.** The account driving this pipeline's Claude API calls has been over its usage quota for the duration of this baseline work (see [`docs/EVALUATION_REPORT.md`](EVALUATION_REPORT.md) § 5), so none of the figures here come from a live pipeline run. Two kinds of numbers appear instead, and they are not interchangeable:

- **Structural estimates** (§ 1, § 2): computed by running the pipeline's actual prompt-construction code against real data already in this repo (a real job posting, a real tailored resume, the real master resume) and counting characters — no API call involved. Token counts from character counts use a chars÷4 heuristic, which is a rough approximation of Anthropic's real tokenizer, not a substitute for one. These numbers are reproducible right now by anyone cloning this repo; they will not match a real tokenizer count exactly, but they're accurate to the right order of magnitude.
- **Measured data** (the instrumentation added by this work): `data/llm_usage_log.jsonl` and `data/pipeline_stage_log.jsonl` are currently empty by design — a log contaminated with mocked-test artifacts from earlier development work was found and reset before writing this document, specifically so this baseline reflects "zero real data yet," not fabricated-looking numbers. Real numbers will populate the first time `usage_report.py` / `performance_baseline_report.py` are run after a live pipeline run. Comparing that first real run against this baseline is the actual point of this exercise.

---

## 1. API Usage Instrumentation

Every Claude API call already logs to `data/llm_usage_log.jsonl` via `modules.util.tracked_create`/`tracked_create_async` (added in an earlier round of work). This baseline adds one thing on top: **per-module rollup**, via `modules/performance_baseline.py`'s `compute_api_usage_by_module()`, which maps each call's label to the module that issued it and aggregates:

- number of calls
- input tokens
- output tokens
- total tokens
- estimated cost (USD, from `PRICING_PER_MTOK` in `modules/util.py`)

Run `python3 performance_baseline_report.py` to see this live. Right now, run against the real (reset) log, it reports:

```
No LLM calls logged yet. Run the pipeline, then re-run this report.
```

### Structural cost estimate (per job, one full pass through the pipeline)

Computed by running each module's actual prompt f-string against a real job + real tailored resume from this repo (see § "A note on the numbers below"):

| Call | Calls/job | Input tok (est.) | Output tok (est.) | Cost (est.) |
|---|---:|---:|---:|---:|
| `module2_scoring` | 1 | ~1,375 | ~100 | $0.0056 |
| `module2b_board_review` (4 reviewers, parallel) | 4 | ~1,933 each | ~120 each | $0.0304 |
| `module2b_board_review` (chair) | 1 | ~400 | ~150 | $0.0034 |
| `module3_resume` | 1 | ~2,071 | ~900 | $0.0197 |
| `module3b_resume_board` (5 reviewers, parallel) | 5 | ~1,592 each | ~250 each | $0.0426 |
| `module3b_resume_board` (chief editor) | 1 | ~2,000 | ~700 | $0.0165 |
| `module3b_resume_board` (rewrite pass) | 1 | ~2,500 | ~900 | $0.0210 |
| `module4_coverletter` | 1 | ~1,245 | ~450 | $0.0105 |
| **Total** | **14** | **~25,300** | **~4,930** | **~$0.15** |

The rewrite pass is listed as typical, not exceptional: real stored `resume_scorecard` data shows 41 of 42 board-reviewed resumes (98%) were flagged `ready_to_submit: false` on first pass, triggering it (see `docs/EVALUATION_REPORT.md` § 4 — the date-grounding bug was the dominant cause, not genuinely poor resume quality, but the rewrite call itself still runs and still costs money regardless of why it was triggered).

At ~$0.15/job, the 294 jobs currently in `data/job_queue.json` represent roughly **$44** in API cost if every one of them ran the full pipeline (in practice fewer than that ran the full sequence — most are `filtered_out` before any board or resume-board call happens).

---

## 2. Performance Instrumentation

New in this round of work: `modules.util.track_stage()`, a context manager wrapping the per-job unit of work in every LLM-driving stage (`module1_discovery.discover_jobs`, `module2_scoring.score_job`, `module2b_board_review.run_advisory_board`, `module3_resume.tailor_resume`, `module3b_resume_board.review_resume`, `module4_coverletter.generate_cover_letter`), logging wall-clock duration to `data/pipeline_stage_log.jsonl`. `module5_apply` (human-in-the-loop, includes think time) and `module6_tracker` (no LLM calls) are deliberately not instrumented — timing a human's decision time isn't a measure of the agent's performance.

`modules/performance_baseline.py`'s `compute_stage_timing_by_module()` and `slowest_modules()` turn this into: execution time per stage, which module is slowest on average, and (dividing total stage time by job count) average processing time per job. Run via `python3 performance_baseline_report.py`. Currently:

```
No stage timing logged yet. Run the pipeline, then re-run this report.
```

### Why this can't be estimated structurally the way cost can

Unlike token counts, wall-clock latency isn't derivable from prompt text — it depends on model load, output length, and network conditions at call time. No structural estimate is given here; this section will be filled in from real data on the next live run.

---

## 3. Current Architecture Impact on Cost

- **The resume and system prompts are resent in full on every call, uncached.** `master_resume.txt` (~5,100 characters) is sent whole in `module3_resume` (untruncated) and truncated in `module2_scoring`/`module4_coverletter`; the tailored resume is sent whole to all 5 `module3b_resume_board` reviewers plus the chief editor plus the rewrite pass. None of this is cached — it's identical bytes resent every time.
- ~~**`module2b_board_review` serializes the entire job dict into every reviewer's prompt, and that dict grows over the job's lifecycle.**~~ **Fixed.** `run_advisory_board()` used to call `json.dumps(job, indent=2)` — for a freshly-scored job (typical case), that was ~7,700 characters (~1,900 tokens); for a job that had already accumulated `board_reviews`, `resume_board`, and `resume_scorecard` data from a prior processing pass, the same call serialized to **~46,300 characters (~11,600 tokens) — a 6x difference** for functionally the same reviewer task, purely because the job record carried more accumulated history. It now calls `json.dumps(_posting_context(job), indent=2)`, a fixed five-field extract (`title`/`company`/`location`/`url`/`description`). Measured against the real queue: jobs not yet board-reviewed dropped ~18% (6,765 → 5,546 chars avg.); jobs already carrying full pipeline history — the unbounded-growth case this fix targets — dropped **92.7%** (48,662 → 3,553 chars avg. across 45 jobs). See `ARCHITECTURE.md` → Module 2b prompt context for the full before/after and why this doesn't itself implement caching.
- **Parallel fan-out multiplies input tokens, not just call count.** `module2b`'s 4 reviewers and `module3b`'s 5 reviewers each receive the *entire* shared context independently — 4x and 5x the input tokens of a single call, respectively, for content that's byte-identical across all of them.
- **The resume-board rewrite pass is the common case, not the exception** (98% of resumes in real stored data), and it resends the full original resume plus the full change log — effectively a second `module3_resume`-sized call on top of the board review itself, for almost every job that reaches this stage.

## 4. Current Bottlenecks

- **`module3b_resume_board` is structurally the most expensive and highest-latency stage per job**: 5 parallel reviewers + a chief editor + (98% of the time) a rewrite pass = up to 8 sequential-dependency LLM calls for one job, per the cost table in § 1.
- **`module2b_board_review`'s job-JSON-dump grows unboundedly with job history** (§ 3) — this is a bottleneck that gets *worse* over the life of a job record, not a fixed cost.
- ~~**No retry/backoff on the synchronous calls**~~ **Fixed.** `module2_scoring`, `module3_resume`, `module4_coverletter` previously had no recovery from a transient network blip, timeout, or 5xx — the whole item failed outright, unlike `module3b_resume_board`'s async calls, which already went through `modules.util.with_retry`. All three now go through the new `modules.util.with_retry_sync` (same `classify_error()`-based four-category policy, synchronous). This was flagged as a known limitation in `docs/EVALUATION_REPORT.md` § 5 and restated here because it was also a *latency* bottleneck, not just a reliability one — a failed call with no retry meant redoing the entire item from scratch on the next run. See `ARCHITECTURE.md` → Retry/backoff for the synchronous modules.
- **Discovery (`module1_discovery`) makes sequential, unbatched HTTP requests** with fixed `time.sleep()` delays between LinkedIn pages — real wall-clock cost, but not something prompt caching touches at all (it's not an LLM call).

## 5. Optimization Opportunities

- **Prompt caching** — see § 6 for the specific, structurally-informed plan (not a blanket "turn it on" — the fan-out reviewers need a different treatment than the sequential single-call stages).
- ~~**Stop serializing the whole job dict in `module2b_board_review`.**~~ **Done.** Board reviewers now receive only the job's *posting* fields (title, company, location, url, description) — not `fit_reasons`, `fit_gaps`, or (on a re-run) prior board/resume-board results. See § 3 above for the measured before/after.
- **Investigate whether the resume-board rewrite pass is over-triggering.** § 1 and § 3 both note it fires on 98% of resumes; `docs/EVALUATION_REPORT.md` traces most of that to the (now-fixed) date-grounding bug. Once real post-fix data exists, check whether the rewrite rate actually drops — if it does, this "bottleneck" shrinks on its own without further optimization work.
- ~~**Extend retry/backoff to the synchronous modules**~~ **Done** (§ 4) — a reliability fix that also reduces wasted re-processing latency.

## 6. Prompt Caching: Implementation and Measured Status

Anthropic's prompt cache is a **prefix match**: only content that is byte-identical across calls, and appears before any cache breakpoint, benefits — and a cache entry only becomes readable once the request that wrote it has begun streaming its response. That second detail matters a lot for this codebase's specific shape:

- ~~**Sequential single-call stages across many jobs**~~ **Implemented this round.** `module2_scoring`, `module3_resume`, `module4_coverletter` each loop over up to 294 jobs, one call per job, with the *same* `CANDIDATE_PROFILE` and/or resume content every time. Each now sends that stable content as its own `cache_control`-marked message block, ahead of the job-specific block. See `ARCHITECTURE.md` → Prompt caching for the split design and `tests/test_prompt_caching.py` for the behavior-preservation proof. **This does not yet produce a measurable benefit — see below.**
- **Parallel fan-out reviewers** (`module2b`'s 4, `module3b`'s 5) — unchanged, out of scope this round. These dispatch concurrently via `asyncio.gather`, so within *one job's* reviewer batch, none of them can read a cache another is still writing. The win here would be **across jobs, not within one job's fan-out**: reviewer N's *system prompt* is identical for every job, so caching that specific, smaller piece would still help — not attempted here.
- ~~**`module2b`'s job-JSON-dump defeats caching by construction**~~ **Fixed** (§ 3, § 5) — the job payload is now a fixed-shape, bounded five-field extract, so the same job's content is guaranteed byte-identical across repeat calls. Unrelated to the fan-out limitation above, which still applies.

### Why this doesn't produce a cache hit yet

Anthropic enforces a **minimum cacheable prefix length per model** — a shorter prefix silently doesn't cache even with `cache_control` set (no error, `cache_creation_input_tokens: 0`). `CLAUDE_MODEL` is `claude-sonnet-4-6`, which requires **2048 tokens**. Measured against this repo's real content (chars÷4 estimate, § "A note on the numbers below"):

| Module | Stable prefix content | Estimated tokens | Vs. 2048 minimum |
|---|---|---:|---:|
| `module2_scoring` | system prompt + profile + resume excerpt (3000 chars) | ~917 | 1,131 short |
| `module4_coverletter` | system prompt + profile + resume excerpt (2000 chars) | ~730 | 1,318 short |
| `module3_resume` | system prompt + full master resume (5,132 chars) | ~1,665 | 383 short |

All three fall short — `module3_resume` closest, since it's the only one of the three that sends the *full* master resume rather than a truncated excerpt. This was verified by computing real prefix sizes against `data/master_resume.txt` and `config.CANDIDATE_PROFILE` **before** implementing, specifically so this wasn't discovered only after shipping something that silently does nothing. No content was padded to force the threshold — that would add real per-call token cost for a benefit that isn't guaranteed, which contradicts the point of this work.

**What would activate it, with no further code changes:** the combined stable content growing past 2048 tokens (a longer master resume, a longer candidate profile — plausible over time as either is edited), or `CLAUDE_MODEL` changing to a model with a lower minimum (Sonnet 4.5/4.1/4/3.7 need only 1024 tokens — `module3_resume`'s ~1,665 would clear that bar today).

### Projected impact, if the minimum is cleared (not a measurement)

Using Anthropic's published cache economics for `claude-sonnet-4-6` (cache write 1.25x input price, cache read 0.1x input price — see `PRICING_PER_MTOK` in `modules/util.py`) applied to `module2_scoring`'s ~841-token candidate-profile-and-resume block across a 294-job batch, *if* that block cleared the minimum: 1 write (~$0.00315) + 293 reads (~$0.0738) ≈ $0.077 total, versus ~$0.741 uncached — roughly a 90% reduction on that portion of the call, ~$0.66 saved across the batch. This is a **projection from published pricing, not a measurement** — presented to show the mechanism's shape, not as an expected outcome, since it doesn't apply until the minimum is cleared.

### What's actually measurable right now

Nothing. Two independent reasons: (1) the account is still over its usage quota as of this writing (see `docs/EVALUATION_REPORT.md` § 5 / `ARCHITECTURE.md` → Evaluation Recovery Workflow, reset 2026-08-01), so no live calls of any kind are possible; (2) even once quota resets, § "Why this doesn't produce a cache hit yet" above means a live run would show `cache_creation_input_tokens: 0` and `cache_read_input_tokens: 0` for all three modules regardless — real content is currently too small. `modules/performance_baseline.py`'s `compute_api_usage_by_module()` now sums both fields per module (previously logged by `tracked_create`/`tracked_create_async` but not rolled up), so the day either constraint changes, `python3 performance_baseline_report.py`'s `cache_wr`/`cache_rd` columns will show it immediately with no further code changes — this section should be the first thing updated with real numbers once that happens.

---

*See also: [`ARCHITECTURE.md`](../ARCHITECTURE.md) for the underlying design of the instrumentation this baseline is built on, and [`docs/EVALUATION_REPORT.md`](EVALUATION_REPORT.md) for the broader evaluation this baseline extends.*
