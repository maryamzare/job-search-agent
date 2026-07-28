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
- **No retry/backoff on the synchronous calls** (`module2_scoring`, `module3_resume`, `module4_coverletter`) — a transient network blip during any of these fails the whole item with no recovery, unlike `module3b_resume_board`'s async calls, which go through `modules.util.with_retry`. This was already flagged as a known limitation in `docs/EVALUATION_REPORT.md` § 5; it's restated here because it's also a *latency* bottleneck, not just a reliability one — a failed call with no retry means redoing the entire item from scratch on the next run.
- **Discovery (`module1_discovery`) makes sequential, unbatched HTTP requests** with fixed `time.sleep()` delays between LinkedIn pages — real wall-clock cost, but not something prompt caching touches at all (it's not an LLM call).

## 5. Optimization Opportunities

- **Prompt caching** — see § 6 for the specific, structurally-informed plan (not a blanket "turn it on" — the fan-out reviewers need a different treatment than the sequential single-call stages).
- ~~**Stop serializing the whole job dict in `module2b_board_review`.**~~ **Done.** Board reviewers now receive only the job's *posting* fields (title, company, location, url, description) — not `fit_reasons`, `fit_gaps`, or (on a re-run) prior board/resume-board results. See § 3 above for the measured before/after.
- **Investigate whether the resume-board rewrite pass is over-triggering.** § 1 and § 3 both note it fires on 98% of resumes; `docs/EVALUATION_REPORT.md` traces most of that to the (now-fixed) date-grounding bug. Once real post-fix data exists, check whether the rewrite rate actually drops — if it does, this "bottleneck" shrinks on its own without further optimization work.
- **Extend retry/backoff to the synchronous modules** (§ 4) — a reliability fix that also reduces wasted re-processing latency.

## 6. Expected Impact of Prompt Caching

Anthropic's prompt cache is a **prefix match**: only content that is byte-identical across calls, and appears before any cache breakpoint, benefits — and a cache entry only becomes readable once the request that wrote it has begun streaming its response. That second detail matters a lot for this codebase's specific shape:

- **Sequential single-call stages across many jobs** (`module2_scoring`, `module3_resume`, `module4_coverletter`) — these loop over up to 294 jobs, each making one call, with the *same* `CANDIDATE_PROFILE` and (for scoring/cover-letter) the same truncated resume excerpt every time. This is the clean caching win: mark that stable content with `cache_control` and, within a single batch run, the 2nd through Nth calls read from cache instead of paying full input price. This is the highest-confidence, lowest-risk place to start.
- **Parallel fan-out reviewers** (`module2b`'s 4, `module3b`'s 5) — these are dispatched concurrently via `asyncio.gather`, so within *one job's* reviewer batch, none of them can read a cache another is still writing (per Anthropic's own documented caching behavior — a cache entry is only readable after the first response begins streaming, and these all start at once). The win here is **across jobs, not within one job's fan-out**: reviewer N's *system prompt* is identical for every job (it's a fixed rubric), so caching that specific, smaller piece would still help, just not to the degree the résumé/job content would if it were restructured into the cacheable portion.
- ~~**`module2b`'s job-JSON-dump defeats caching by construction, independent of the fix above**~~ **Fixed** (§ 3, § 5) — the job payload is now a fixed-shape, bounded five-field extract instead of the full accumulating dict, so the same job's content is now guaranteed byte-identical across repeat calls (a necessary precondition for any cache hit here) and small regardless of how much pipeline history the job carries. This doesn't add caching by itself — no `cache_control` was touched — but it removes what was previously an independent, structural block on ever caching this call site at all. The fan-out limitation two bullets up (reviewers can't cache off each other within one job's parallel dispatch) is unrelated to this fix and still applies.

**What to expect, directionally:** the sequential stages should see a substantial fraction of their input-token cost move from full price to the ~0.1x cache-read rate on every call after the first, within a batch run. The fan-out stages will see a smaller win unless restructured. No specific percentage is claimed here — that number should come from comparing a real `usage_report.py`/`performance_baseline_report.py` run before and after, exactly as this document's "before" half is designed to make possible.

---

*See also: [`ARCHITECTURE.md`](../ARCHITECTURE.md) for the underlying design of the instrumentation this baseline is built on, and [`docs/EVALUATION_REPORT.md`](EVALUATION_REPORT.md) for the broader evaluation this baseline extends.*
