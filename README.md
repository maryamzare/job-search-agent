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
go through `config.py` for centralized model and key management.

## Design notes

- **Quality gates over speed.** Jobs and resumes each pass a "board review" stage —
  multiple Claude reviewers with different rubrics (fit, ATS keywords, red flags) —
  before anything is submitted. A rejected resume comes back with a prioritized
  change log, not just a score.
- **One source of truth.** `data/job_queue.json` holds every job's full lifecycle:
  score, reasons, gaps, board verdicts, resume versions, application dates.
- **Human in the loop.** The agent drafts and recommends; the human applies. Every
  generated document is written to `outputs/` for review before it goes anywhere.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here

# add your data (real files are gitignored)
cp data/master_resume.example.txt data/master_resume.txt   # then edit with your resume
cp data/job_queue.example.json data/job_queue.json

python main.py --help
```

## Tech stack

Python · Claude API (`claude-sonnet-4-6`) · requests / BeautifulSoup for scraping ·
JSON state store

---

Built by [Maryam Zare](https://www.linkedin.com/in/maryam-zare) — Senior Technical
Program Manager (platforms, data systems, AI enablement), Seattle.
