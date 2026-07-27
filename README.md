# Job Search Agent

An AI-powered pipeline that automates a technical job search end to end — discovery,
fit scoring, resume tailoring, cover letters, and application tracking — built on the
[Claude API](https://docs.anthropic.com/).

I built this as a working tool for my own job search (it has processed 200+ real
postings) and as a demonstration of how I think about program automation: break an
ambiguous process into owned stages, make state visible, and put quality gates
between stages.

## How it works

```
discover → score → board review → tailor resume → resume board → cover letter → apply → track
```

| Stage | Module | What it does |
|-------|--------|--------------|
| 1 | `modules/module1_discovery.py` | Scrapes job postings from LinkedIn, Indeed, Greenhouse, and company boards |
| 2 | `modules/module2_scoring.py` | Uses Claude to score each job (0–100) against the candidate profile; filters below threshold |
| 2b | `modules/module2b_board_review.py` | A multi-reviewer "hiring board" that debates borderline jobs before an apply decision |
| 3 | `modules/module3_resume.py` | Tailors the master resume to a specific job description |
| 3b | `modules/module3b_resume_board.py` | ATS keyword check + scorecard review of the tailored resume before submission |
| 4 | `modules/module4_coverletter.py` | Generates a role-specific cover letter |
| 5 | `modules/module5_apply.py` | Automates or guides application submission |
| 6 | `modules/module6_tracker.py` | Tracks every job's status in a JSON queue (discovered → shortlisted → applied → interviewing) |

`main.py` is the CLI orchestrator that wires the stages together. All Claude API calls
go through `config.py` for centralized model and key management. `modules/util.py`
centralizes cross-cutting concerns shared across every stage: queue I/O, Claude client
construction, LLM-call cost/latency instrumentation, retry policy, and JSON-response
parsing.

## Design notes

- **Quality gates over speed.** Jobs and resumes each pass a "board review" stage —
  multiple Claude reviewers with different rubrics (fit, ATS keywords, red flags) —
  before anything is submitted. A rejected resume comes back with a prioritized
  change log, not just a score.
- **One source of truth.** `data/job_queue.json` holds every job's full lifecycle:
  score, reasons, gaps, board verdicts, resume versions, application dates.
- **Human in the loop.** The agent drafts and recommends; the human applies. Every
  generated document is written to `outputs/` for review before it goes anywhere.

## Evaluating the agent

This isn't a "build it and hope" pipeline — its own output is periodically evaluated
against real, stored data rather than assumption: resume-board scorecards (ATS keyword
coverage, impact language, red-flag verdicts) are cross-checked against actual generated
resumes and the master resume they're tailored from, to catch both hallucinated claims
and false-positive review flags. That evaluation process has already surfaced and fixed
real defects — most notably a systematic false "future-dated employment" flag that was
triggering unnecessary resume rewrites on ~95% of resume-board reviews, traced to
reviewer prompts having no way to know the actual current date (see `ARCHITECTURE.md` →
Date-grounding for reviewers).

## Reliability & observability

- **Retry strategy.** `modules/util.py`'s `classify_error()` splits every Claude API
  failure into four categories handled differently: genuine rate limits (HTTP 429) retry
  with exponential backoff and honor a `Retry-After` header when present; account-level
  quota exhaustion fails fast with a clear notification instead of retrying a condition
  that won't resolve for hours to days; plain transient failures (network errors,
  timeouts, 5xx) retry with backoff; everything else (auth failures, invalid requests,
  permission errors) fails immediately with no retry. See `ARCHITECTURE.md` → Retry
  policy for why rate limiting and quota exhaustion need opposite handling despite
  looking similar.
- **API failure recovery.** When a live evaluation is blocked by account-level quota
  exhaustion, `modules/eval_recovery.py` persists the failed attempt (what to re-check,
  the "before" snapshot, and the reset time parsed from Anthropic's own error message) to
  disk, and `evaluation_recovery.py check-and-run` — safe to invoke on a schedule (cron
  or otherwise) — automatically re-runs the evaluation once the quota resets and writes a
  before/after comparison report. See `ARCHITECTURE.md` → Evaluation recovery workflow.
- **Pipeline lifecycle instrumentation.** Every job accrues real timestamps as it moves
  through the pipeline (`shortlisted_at`, `application_submitted_at`,
  `closed_or_expired_at`), and `pipeline_timing_report.py` turns them into concrete
  metrics: average/median time from shortlist to application, jobs closed before ever
  being applied to, and high-scoring jobs sitting past a delay threshold — turning "did I
  move fast enough on good jobs?" from an anecdote into a measured, reproducible answer.
  See `ARCHITECTURE.md` → Pipeline timing instrumentation.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here

# add your data (real files are gitignored)
cp data/master_resume.example.txt data/master_resume.txt   # then edit with your resume
cp data/job_queue.example.json data/job_queue.json

python main.py --help

# run the test suite (79 tests, no API key or network access required)
python3 -m unittest discover -s tests -v

# other tooling
python3 usage_report.py             # LLM cost/latency summary
python3 pipeline_timing_report.py   # shortlist-to-apply timing report
python3 evaluation_recovery.py status
```

## Architecture & documentation

`ARCHITECTURE.md` documents every non-obvious design decision in this repo — the
problem it solves, the design chosen, and why — including the retry policy, the
evaluation recovery workflow, date-grounding for reviewers, and pipeline lifecycle
instrumentation. `CHANGELOG.md` tracks what changed and why, in the order it happened.

**[docs/EVALUATION_REPORT.md](docs/EVALUATION_REPORT.md)** is a structured evaluation of
this agent's real output and architecture: before/after reliability metrics, an
architecture review (prompt strategy, tool usage, failure handling, data flow), the
engineering decisions behind each major fix (problem → decision → tradeoff → result),
current limitations, and the roadmap ahead.

## Tech stack

Python · Claude API (`claude-sonnet-4-6`) · requests / BeautifulSoup for scraping ·
JSON state store · `unittest`-based test suite

---

Built by [Maryam Zare](https://www.linkedin.com/in/maryam-zare) — Senior Technical
Program Manager (platforms, data systems, AI enablement), Seattle.
