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

## Discovery sources

The `discover` command searches exactly five sources, each behind a `_safe(...)`
wrapper so an HTTP error, malformed JSON, or a changed page schema in one is
logged and skipped without stopping the rest:

| # | Source | Adapter | Notes |
|---|---|---|---|
| 1 | LinkedIn | `search_jobs_linkedin` | guest jobs API; keeps `LINKEDIN_LOOKBACK_HOURS` |
| 2 | Apple Careers | `search_jobs_apple` | the old `POST /api/role/search` now 301s to a not-found page; the live path is the `jobs.apple.com/en-us/search` page's embedded `__staticRouterHydrationData` (`loaderData.search`), paginated via `totalRecords`, plus the detail page's `loaderData.jobDetails.jobsData` for the full JD (summary + description + min/preferred qualifications). Falls back to the search summary if the detail fetch fails. |
| 3 | Anthropic | `search_jobs_greenhouse` | Greenhouse boards API (`boards-api.greenhouse.io`) |
| 4 | OpenAI | `search_jobs_ashby` | Ashby public job-board API (`api.ashbyhq.com/posting-api/job-board/openai`); OpenAI is not on Greenhouse. Ashby gives a city-only `location`, so the country comes from `address.postalAddress.addressCountry` and `isRemote` is honored. |
| 5 | Oracle Careers | `search_jobs_oracle` | Oracle Recruiting Cloud: the **list** endpoint (`recruitingCEJobRequisitions`, paginated by `offset`/`TotalJobsCount`) for the listing, then the **per-requisition detail** endpoint (`recruitingCEJobRequisitionDetails`) for the complete description — the list endpoint returns `ExternalDescriptionStr` / `ExternalResponsibilitiesStr` / `ExternalQualificationsStr` as `null`, so the description is built from summary + responsibilities + qualifications off the detail record. `ORACLE_API_BASE` + `ORACLE_SITE_NUMBER` are the only tenant-specific constants. |

All other Greenhouse companies (Scale, Figma, Notion, Stripe, Databricks,
Cohere, Mistral, Perplexity) and every Lever company (Netflix, Canva, Airtable,
Vercel) were removed, along with the Lever adapter.

Every adapter applies the same title-keyword filter and the shared
`_location_matches` filter (explicit country fields, `United States`/`USA`
forms, and word-boundary US state codes — never a bare `us` substring, which
had matched Houston, Belarus, and Australia). Each returns `title`, `company`,
`location`, canonical `url`, `apply_url`, and the full `description`; records
without an ID/URL are dropped rather than turned into a URL with an empty ID.
`deduplicate()` keys on the canonical URL (posting identity), so two
requisitions that share a company and title but differ by URL are both kept.
Scoring and résumé generation are downstream and separate — discovery never
triggers them.

## Posting identity and artifacts

Discovery deduplicates by canonical URL. Company/title is not an identity because employers repost roles. Generated materials use a filename derived from company, title, and a SHA-256 digest of URL plus description. A changed posting therefore receives new materials.

## Measurement

The tracker records `shortlisted_at`, `application_submitted_at`, and `interviewing_at`. `pipeline_timing_report.py` reports shortlist-to-application delay and jobs lost before submission. Monthly application and interview conversion should be calculated from these timestamps, not current status counts alone.
