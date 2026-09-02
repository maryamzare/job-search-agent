# Job Search Agent v2

A lean Python agent optimized for one outcome: generating interviews from timely, well-matched applications.

## Pipeline

```text
discover → score → tailor resume → apply → track outcomes
```

| Stage | Module | Responsibility |
|---|---|---|
| Discovery | `modules/module1_discovery.py` | Finds recent LinkedIn, Greenhouse, and Lever postings |
| Scoring | `modules/module2_scoring.py` | Produces one validated 0–100 fit score; failures remain retryable |
| Resume | `modules/module3_resume.py` | Creates a posting-specific tailored resume |
| Cover letter | `modules/module4_coverletter.py` | Optional generation when an application requires one |
| Apply | `modules/module5_apply.py` | Resumes incomplete applications and prevents submission without a resume |
| Tracking | `modules/module6_tracker.py` | Records application and interview lifecycle timestamps |

V2 deliberately removes the job advisory board and resume review board. Those layers added 11–12 model calls per application, duplicated existing prompts, and had no measured evidence of improving interview conversion.

## State and outputs

- Jobs live in `data/job_queue.json` under the top-level `jobs` key.
- Tailored resumes go to `outputs/tailored_resumes/`.
- Optional cover letters go to `outputs/cover_letters/`.
- Artifact filenames contain a posting fingerprint so a repost cannot silently reuse stale material.
- Failed model parsing never filters a job; it records `scoring_error` and leaves the job retryable.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here

cp data/master_resume.example.txt data/master_resume.txt
cp data/job_queue.example.json data/job_queue.json
```

## Usage

```bash
python3 main.py run          # discover, score, and tailor resumes
python3 main.py apply        # finish in-progress jobs first, then highest score
python3 main.py coverletter  # optional; only when required
python3 main.py status
python3 pipeline_timing_report.py
```

Tests make no live API calls:

```bash
python3 -m unittest discover -s tests -v
```

The original architecture assessment is retained as historical context in
[the evaluation report](docs/EVALUATION_REPORT.md); v2's current decisions are documented in `ARCHITECTURE.md`.

## Throughput rules

1. Apply to high-fit live roles quickly; document production is not success.
2. An incomplete application must remain visible as `in_progress`.
3. Never convert an API or parsing failure into a rejection.
4. Track timestamps so application-to-interview conversion can be measured by month.

Built by [Maryam Zare](https://www.linkedin.com/in/maryamzare).
