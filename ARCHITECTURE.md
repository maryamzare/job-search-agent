# Architecture v2

## Design objective

Optimize for interviews per month. Every stage must either improve match quality, produce a required application artifact, submit an application, or measure conversion.

```text
sources → job_queue.json → validated fit score → posting-specific resume → human submission → outcome timestamps
```

## Deliberate deletions

The job advisory board and resume advisory board were removed. Scoring already decides match quality, while the resume prompt already covers ATS terms, relevance, measurable impact, and factuality. Repeating those decisions across specialist personas increased latency, cost, state complexity, and false-gate exposure without interview-conversion evidence.

Cover letters remain an explicit command because some applications require them, but are not part of the default run.

## Failure semantics

- Scoring output must be valid JSON with a numeric score from 0 through 100.
- API, parsing, and schema errors retain `status: discovered` and store `scoring_error`; they never become score zero.
- Queue state is saved after every completed scoring or application decision.
- `in_progress` applications are processed first on the next apply run.
- Application cannot proceed without the resume fingerprinted to that posting.

## Posting identity and artifacts

Discovery deduplicates by canonical URL. Company/title is not an identity because employers repost roles. Generated materials use a filename derived from company, title, and a SHA-256 digest of URL plus description. A changed posting therefore receives new materials.

## Measurement

The tracker records `shortlisted_at`, `application_submitted_at`, and `interviewing_at`. `pipeline_timing_report.py` reports shortlist-to-application delay and jobs lost before submission. Monthly application and interview conversion should be calculated from these timestamps, not current status counts alone.
