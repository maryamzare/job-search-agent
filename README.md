# Job Search Agent v2

A lean Python agent optimized for one outcome: generating interviews from timely, well-matched applications.

## Pipeline

```text
discover → score → (stop) → resume-list → resume-one <job-id> → apply → track outcomes
```

| Stage | Module | Responsibility |
|---|---|---|
| Discovery | `modules/module1_discovery.py` | Finds recent LinkedIn, Greenhouse, and Lever postings |
| Scoring | `modules/module2_scoring.py` | Produces one validated 0–100 fit score; failures remain retryable |
| Resume | `modules/module3_resume.py` | Generates ONE validated `.docx` resume for ONE job, on request |
| Cover letter | `modules/module4_coverletter.py` | Optional generation when an application requires one |
| Apply | `modules/module5_apply.py` | Resumes incomplete applications and prevents submission without a resume |
| Tracking | `modules/module6_tracker.py` | Records application and interview lifecycle timestamps |

V2 deliberately removes the job advisory board and resume review board. Those layers added 11–12 model calls per application, duplicated existing prompts, and had no measured evidence of improving interview conversion.

### Resume generation is one job at a time

There is no batch resume path. `python3 main.py run` stops after scoring, and
bare `python3 main.py resume` only prints instructions. To produce a resume:

```bash
python3 main.py resume-list                       # eligible job IDs (read-only, no API call)
python3 main.py resume-one <job-id>               # one validated .docx for one job
python3 main.py resume-one <job-id> --with-summary  # include a Summary section (omitted by default)
python3 main.py resume-one <job-id> --replace     # regenerate; the old file is moved to superseded/
```

`resume-one`:

- resolves exactly one job by an exact `job_artifact_key` match (missing / unknown
  / ineligible / ambiguous IDs exit non-zero and make no API call);
- treats `data/career_profile_source_of_truth.md` as the **only** source of facts
  (historical `master_resume*.txt` files are not inputs here);
- tailors to the job description, then runs an authenticity + formatting pass,
  then deterministic + semantic validation (3 model calls on the success path);
- on any unresolved issue, writes only `outputs/resume_reviews/<job-id>.review.md`
  and saves no resume;
- on success, renders one ATS-safe single-column `.docx` (no tables, text boxes,
  images, headers, or footers) to `outputs/tailored_resumes/<job-id>.docx` via a
  temp file that must reopen cleanly before it is promoted;
- never silently overwrites an existing final resume.

Contact details for the document come from `data/contact_block.txt`
(git-ignored; copy `data/contact_block.example.txt`). They are never sent to
model calls and never printed in logs.

## State and outputs

- Jobs live in `data/job_queue.json` under the top-level `jobs` key.
- Tailored resumes go to `outputs/tailored_resumes/`.
- Optional cover letters go to `outputs/cover_letters/`.
- Artifact filenames contain a posting fingerprint so a repost cannot silently reuse stale material.
- Failed model parsing never filters a job; it records `scoring_error` and leaves the job retryable.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # includes python-docx for resume-one
export ANTHROPIC_API_KEY=your_key_here

cp data/master_resume.example.txt data/master_resume.txt
cp data/job_queue.example.json data/job_queue.json
cp data/contact_block.example.txt data/contact_block.txt && chmod 600 data/contact_block.txt
```

## Usage

```bash
python3 main.py run          # discover + score, then STOP (resume step is manual)
python3 main.py resume-list  # eligible job IDs for a tailored resume (read-only)
python3 main.py resume-one <job-id>   # one validated .docx resume for one job
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
