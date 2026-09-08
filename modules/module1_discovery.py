"""
Module 1 — Job Discovery

The automatic `discover` command searches exactly five sources:

  1. LinkedIn        — guest jobs API (keeps LINKEDIN_LOOKBACK_HOURS)
  2. Apple Careers   — jobs.apple.com search + detail pages (embedded
                       __staticRouterHydrationData JSON), paginated
  3. Anthropic       — Greenhouse boards API
  4. OpenAI          — Ashby public job-board API (OpenAI is not on Greenhouse)
  5. Oracle Careers  — Oracle Recruiting Cloud public API: list + per-req
                       detail endpoint for the complete description, paginated

Every source runs inside `_safe(...)`, so a failure in one (HTTP error, bad
JSON, changed schema) is logged and skipped without stopping the others.

This module only discovers and stores postings (title, location, canonical
URL, full description). It never scores jobs or generates résumés.
"""

import json
import math
import re
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from config import TARGET_ROLES, TARGET_LOCATIONS, JOB_QUEUE_PATH, LINKEDIN_LOOKBACK_HOURS
from modules.util import load_queue as _load_queue, save_queue as _save_queue, track_stage

# --- Greenhouse: Anthropic only. OpenAI 404s on Greenhouse; see ASHBY_COMPANIES.
GREENHOUSE_COMPANIES = {
    "anthropic": "Anthropic",
}

# --- Ashby: OpenAI's real public board.
ASHBY_COMPANIES = {
    "openai": "OpenAI",
}

# --- Apple Careers (jobs.apple.com). The old POST /api/role/search endpoint
# now 301s to apple.com/pagenotfound; the live path is the search page's
# embedded hydration JSON, plus the detail page for the full description.
APPLE_SEARCH_URL = "https://jobs.apple.com/en-us/search"
APPLE_DETAIL_URL = "https://jobs.apple.com/en-us/details/{position_id}/{slug}"
APPLE_SEARCH_QUERIES = ["program manager", "engineering program manager", "product manager"]
APPLE_PAGE_SIZE = 20          # Apple returns 20 results/page
APPLE_MAX_PAGES = 5           # up to 100 results per query

# --- Oracle Recruiting Cloud (ORC) public API for careers.oracle.com.
# host + siteNumber are Oracle-tenant specific; if the corporate site
# migrates, only these two constants change.
ORACLE_API_BASE = "https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest"
ORACLE_LIST_API = f"{ORACLE_API_BASE}/recruitingCEJobRequisitions"
ORACLE_DETAIL_API = f"{ORACLE_API_BASE}/recruitingCEJobRequisitionDetails"
ORACLE_SITE_NUMBER = "CX_45001"
ORACLE_JOB_URL = "https://careers.oracle.com/en/sites/jobsearch/job/{req_id}"
ORACLE_SEARCH_QUERIES = ["program manager", "technical program manager", "product manager"]
ORACLE_PAGE_SIZE = 50
ORACLE_MAX_PAGES = 4          # up to 200 results per query

# Keywords that must appear in the job title to be considered relevant.
TITLE_KEYWORDS = [
    "program manager", "tpm", "product manager", "engineering manager",
    "technical program", "ai program", "ml program",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}
_US_COUNTRY = {"US", "USA", "U.S.", "U.S.A.", "UNITED STATES", "UNITED STATES OF AMERICA"}
# Word-boundary allow list — no bare "us" substring (that matched Houston,
# Belarus, Australia, ...). Seattle-area metros stay since TARGET_LOCATIONS
# is Seattle-centric and LinkedIn often gives "Greater Seattle Area".
_LOC_ALLOW_RE = re.compile(
    r"\b(united states|u\.?s\.?a\.?|u\.?s\.?|remote|hybrid|anywhere|nationwide|"
    r"seattle|bellevue|kirkland|redmond)\b",
    re.IGNORECASE,
)
_US_STATE_IN_LOC_RE = re.compile(r"(?:^|,)\s*([A-Za-z]{2})(?:\s*(?:,|$|\s-))")


# ── LinkedIn ──────────────────────────────────────────────────────────────────

def _fetch_linkedin_page(role: str, location: str, start: int, hours: int | None = None) -> list[dict]:
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote_plus(role)}&location={quote_plus(location)}&start={start}"
    )
    if hours:
        url += f"&f_TPR=r{hours * 3600}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"  [linkedin] HTTP {resp.status_code} for '{role}' / '{location}'")
            return []
    except requests.RequestException as e:
        print(f"  [linkedin] Request error: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.find_all("li")
    jobs = []
    for card in cards:
        title_el = card.find(class_="base-search-card__title")
        company_el = card.find(class_="base-search-card__subtitle")
        location_el = card.find(class_="job-search-card__location")
        link_el = card.find("a", class_="base-card__full-link")
        if not (title_el and company_el and link_el):
            continue
        title = title_el.get_text(strip=True)
        if not _title_matches(title):
            continue
        job_url = link_el["href"].split("?")[0]
        jobs.append({
            "title": title,
            "company": company_el.get_text(strip=True),
            "location": location_el.get_text(strip=True) if location_el else location,
            "url": job_url,
            "apply_url": job_url,
            "description": _fetch_linkedin_description(job_url),
        })
        time.sleep(0.5)
    return jobs


def _fetch_linkedin_description(job_url: str) -> str:
    # Extract job ID from URL. LinkedIn serves both bare IDs
    # (.../view/1234567890/) and slugged IDs (.../view/some-title-1234567890),
    # so the ID must be matched at the end of the path, not right after
    # "/view/" - a right-after-"/view/" match misses every slugged URL,
    # which is most of what LinkedIn's search results actually return.
    match = re.search(r"(\d+)/?$", job_url)
    if not match:
        return ""
    job_id = match.group(1)
    detail_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    try:
        resp = requests.get(detail_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        desc_el = soup.find(class_="show-more-less-html__markup")
        if desc_el:
            return desc_el.get_text(separator="\n", strip=True)
        desc_el = soup.find("div", {"class": re.compile(r"description")})
        return desc_el.get_text(separator="\n", strip=True) if desc_el else ""
    except requests.RequestException:
        return ""


def search_jobs_linkedin(role: str, location: str, pages: int = 2, hours: int | None = None) -> list[dict]:
    print(f"[discovery] LinkedIn: '{role}' in '{location}'" + (f" (past {hours}h)" if hours else ""))
    jobs = []
    for page in range(pages):
        batch = _fetch_linkedin_page(role, location, start=page * 25, hours=hours)
        jobs.extend(batch)
        if not batch:
            break
        time.sleep(1.5)
    print(f"  → {len(jobs)} relevant listings")
    return jobs


# ── Greenhouse (Anthropic) ────────────────────────────────────────────────────

def search_jobs_greenhouse(company_slug: str, company_name: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"  [greenhouse] {company_name}: HTTP {resp.status_code}")
            return []
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  [greenhouse] {company_name}: {e}")
        return []

    jobs = []
    for item in data.get("jobs", []):
        title = item.get("title", "")
        if not _title_matches(title):
            continue
        location = (item.get("location") or {}).get("name", "")
        if not _location_matches(location):
            continue
        url = item.get("absolute_url", "")
        if not url:
            print(f"  [greenhouse] {company_name}: skipped a record with no URL")
            continue
        content = BeautifulSoup(item.get("content", ""), "html.parser").get_text(
            separator="\n", strip=True
        )
        jobs.append({
            "title": title,
            "company": company_name,
            "location": location,
            "url": url,
            "apply_url": url,
            "description": content,
        })
    print(f"  [greenhouse] {company_name}: {len(jobs)} relevant listings")
    return jobs


# ── Ashby (OpenAI) ────────────────────────────────────────────────────────────

def search_jobs_ashby(company_slug: str, company_name: str) -> list[dict]:
    url = (
        f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}"
        "?includeCompensation=true"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"  [ashby] {company_name}: HTTP {resp.status_code}")
            return []
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  [ashby] {company_name}: {e}")
        return []

    jobs = []
    for item in data.get("jobs", []):
        title = item.get("title", "")
        if not _title_matches(title):
            continue
        country = (
            ((item.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        )
        location = item.get("location") or ""
        if not (item.get("isRemote") or _location_matches(location, country=country)):
            continue
        job_url = item.get("jobUrl") or item.get("applyUrl") or ""
        if not job_url:
            print(f"  [ashby] {company_name}: skipped a record with no URL")
            continue
        description = item.get("descriptionPlain") or ""
        if not description and item.get("descriptionHtml"):
            description = BeautifulSoup(item["descriptionHtml"], "html.parser").get_text(
                separator="\n", strip=True
            )
        jobs.append({
            "title": title,
            "company": company_name,
            "location": location or (country or ""),
            "url": job_url,
            "apply_url": item.get("applyUrl") or job_url,
            "description": description,
        })
    print(f"  [ashby] {company_name}: {len(jobs)} relevant listings")
    return jobs


# ── Apple Careers ─────────────────────────────────────────────────────────────

def _apple_hydration(html: str) -> dict | None:
    """Parse `window.__staticRouterHydrationData = JSON.parse("...")`."""
    m = re.search(r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((".*?")\);', html, re.S)
    if not m:
        return None
    try:
        return json.loads(json.loads(m.group(1)))  # JS string literal -> JSON text -> obj
    except (json.JSONDecodeError, ValueError):
        return None


def _apple_detail_description(position_id: str, slug: str) -> str:
    url = APPLE_DETAIL_URL.format(position_id=position_id, slug=slug or "job")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return ""
    except requests.RequestException:
        return ""
    data = _apple_hydration(resp.text) or {}
    job = (((data.get("loaderData") or {}).get("jobDetails") or {}).get("jobsData")) or {}
    parts = [job.get(k, "") for k in
             ("jobSummary", "description", "minimumQualifications",
              "preferredQualifications", "keyQualifications", "additionalRequirements")]
    return "\n\n".join(
        BeautifulSoup(p, "html.parser").get_text("\n", strip=True) for p in parts if p
    ).strip()


def _apple_page(query: str, page: int) -> tuple[list[dict], int]:
    """Return (results, total_records) for one search page."""
    try:
        resp = requests.get(
            APPLE_SEARCH_URL, headers=HEADERS, timeout=20,
            params={"search": query, "sort": "newest", "page": page},
        )
        if resp.status_code != 200:
            print(f"  [apple] '{query}' p{page}: HTTP {resp.status_code}")
            return [], 0
    except requests.RequestException as e:
        print(f"  [apple] '{query}' p{page}: {e}")
        return [], 0
    data = _apple_hydration(resp.text)
    if not data:
        print(f"  [apple] '{query}' p{page}: no hydration data")
        return [], 0
    search = (data.get("loaderData") or {}).get("search") or {}
    return search.get("searchResults") or [], int(search.get("totalRecords") or 0)


def search_jobs_apple(query: str) -> list[dict]:
    first, total = _apple_page(query, 1)
    pages = min(APPLE_MAX_PAGES, max(1, math.ceil(total / APPLE_PAGE_SIZE))) if total else 1
    results = list(first)
    for p in range(2, pages + 1):
        time.sleep(0.5)
        more, _ = _apple_page(query, p)
        if not more:
            break
        results.extend(more)

    jobs, skipped = [], 0
    for item in results:
        title = item.get("postingTitle") or item.get("title") or ""
        if not _title_matches(title):
            continue
        locs = item.get("locations") or []
        location = ", ".join(l.get("name", "") for l in locs if l.get("name"))
        country = next((l.get("countryName") for l in locs if l.get("countryName")), None)
        if not _location_matches(location, country=country):
            continue
        position_id = str(item.get("positionId") or "").strip()
        if not position_id:
            skipped += 1
            continue  # never build a URL with an empty ID
        slug = item.get("transformedPostingTitle") or ""
        job_url = APPLE_DETAIL_URL.format(position_id=position_id, slug=slug or "job")
        description = _apple_detail_description(position_id, slug)
        if not description:
            description = BeautifulSoup(item.get("jobSummary", ""), "html.parser").get_text(
                "\n", strip=True
            ).strip()
        jobs.append({
            "title": title,
            "company": "Apple",
            "location": location,
            "url": job_url,
            "apply_url": job_url,
            "description": description,
        })
        time.sleep(0.3)
    msg = f"  [apple] '{query}': {len(jobs)} relevant listings"
    if skipped:
        msg += f" ({skipped} record(s) skipped: no position id)"
    print(msg)
    return jobs


# ── Oracle Careers ────────────────────────────────────────────────────────────

def _oracle_list_page(query: str, offset: int) -> tuple[list[dict], int]:
    finder = (
        f"findReqs;siteNumber={ORACLE_SITE_NUMBER},"
        f'keyword="{query}",limit={ORACLE_PAGE_SIZE},offset={offset},'
        "sortBy=POSTING_DATES_DESC"
    )
    params = {
        "onlyData": "true",
        "expand": "requisitionList.secondaryLocations",
        "finder": finder,
    }
    try:
        resp = requests.get(ORACLE_LIST_API, headers=HEADERS, params=params, timeout=20)
        if resp.status_code != 200:
            print(f"  [oracle] '{query}' offset {offset}: HTTP {resp.status_code}")
            return [], 0
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  [oracle] '{query}' offset {offset}: {e}")
        return [], 0
    reqs, total = [], 0
    for block in data.get("items", []):
        reqs.extend(block.get("requisitionList", []))
        total = max(total, int(block.get("TotalJobsCount") or 0))
    return reqs, total


def _oracle_full_description(req_id: str) -> str:
    """Complete JD from the per-requisition detail endpoint: overview +
    responsibilities + qualifications (the list endpoint returns these as
    null)."""
    finder = f'ById;Id="{req_id}",siteNumber={ORACLE_SITE_NUMBER}'
    try:
        resp = requests.get(
            ORACLE_DETAIL_API, headers=HEADERS, timeout=20,
            params={"onlyData": "true", "expand": "all", "finder": finder},
        )
        if resp.status_code != 200:
            return ""
        items = resp.json().get("items", [])
    except (requests.RequestException, json.JSONDecodeError):
        return ""
    if not items:
        return ""
    r = items[0]
    sections = [
        r.get("ShortDescriptionStr") or "",
        r.get("ExternalDescriptionStr") or "",
        r.get("ExternalResponsibilitiesStr") or "",
        r.get("ExternalQualificationsStr") or "",
    ]
    out = []
    for html in sections:
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True).strip()
        if text and text not in "\n\n".join(out):
            out.append(text)
    return "\n\n".join(out)


def search_jobs_oracle(query: str) -> list[dict]:
    first, total = _oracle_list_page(query, 0)
    pages = min(ORACLE_MAX_PAGES, max(1, math.ceil(total / ORACLE_PAGE_SIZE))) if total else 1
    reqs = list(first)
    for p in range(1, pages):
        time.sleep(0.4)
        more, _ = _oracle_list_page(query, p * ORACLE_PAGE_SIZE)
        if not more:
            break
        reqs.extend(more)

    jobs, skipped = [], 0
    for item in reqs:
        title = item.get("Title", "")
        if not _title_matches(title):
            continue
        secondary = ", ".join(
            s.get("Name", "") for s in (item.get("secondaryLocations") or []) if s.get("Name")
        )
        location = item.get("PrimaryLocation", "") or secondary
        if secondary and secondary not in location:
            location = f"{location}; {secondary}" if location else secondary
        if not _location_matches(location, country=item.get("PrimaryLocationCountry")):
            continue
        req_id = str(item.get("Id") or "").strip()
        if not req_id:
            skipped += 1
            continue
        description = _oracle_full_description(req_id)
        if not description:
            description = BeautifulSoup(
                item.get("ShortDescriptionStr", ""), "html.parser"
            ).get_text("\n", strip=True).strip()
        job_url = ORACLE_JOB_URL.format(req_id=req_id)
        jobs.append({
            "title": title,
            "company": "Oracle",
            "location": location,
            "url": job_url,
            "apply_url": job_url,
            "description": description,
        })
        time.sleep(0.3)
    msg = f"  [oracle] '{query}': {len(jobs)} relevant listings"
    if skipped:
        msg += f" ({skipped} record(s) skipped: no requisition id)"
    print(msg)
    return jobs


# ── Helpers ───────────────────────────────────────────────────────────────────

def _title_matches(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TITLE_KEYWORDS)


def _location_matches(location: str, country: str | None = None) -> bool:
    """US / remote-friendly location filter. Uses explicit country fields, the
    'United States'/'USA' forms, and word-boundary US state codes — never a
    bare 'us' substring (which matched Houston, Belarus, Australia, ...)."""
    if country and country.strip().upper() in _US_COUNTRY:
        return True
    if not location or not location.strip():
        return True  # no location given -> assume remote/flexible
    loc = location.strip()
    if _LOC_ALLOW_RE.search(loc):
        return True
    for m in _US_STATE_IN_LOC_RE.finditer(loc):
        if m.group(1).upper() in _US_STATES:
            return True
    return False


def deduplicate(jobs: list[dict]) -> list[dict]:
    """Posting identity is the canonical URL (ARCHITECTURE.md). Two
    requisitions with the same company/title but different URLs are two
    postings and both are kept."""
    seen = set()
    unique = []
    for job in jobs:
        url = (job.get("url") or "").strip()
        key = url or (
            job.get("company", "").lower(),
            job.get("title", "").lower(),
            (job.get("description") or "")[:200],
        )
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


def _safe(label: str, fn, *args, **kwargs) -> list[dict]:
    """Run one source. Any failure is logged and turned into an empty result so
    the remaining sources still run."""
    try:
        return fn(*args, **kwargs) or []
    except Exception as e:  # noqa: BLE001 — deliberately broad; one source must not sink the rest
        print(f"  [{label}] source failed, skipping: {type(e).__name__}: {e}")
        return []


# ── Orchestration ─────────────────────────────────────────────────────────────

def discover_jobs() -> list[dict]:
    with track_stage("module1_discovery"):
        raw = []

        # 1. LinkedIn — keeps the configured lookback window.
        for role in TARGET_ROLES:
            for location in TARGET_LOCATIONS:
                if location == "Hybrid":
                    continue  # LinkedIn has no "Hybrid" location filter
                raw.extend(_safe(
                    "linkedin", search_jobs_linkedin, role, location,
                    hours=LINKEDIN_LOOKBACK_HOURS,
                ))
                time.sleep(2)

        # 2. Apple Careers
        print("[discovery] Scanning Apple Careers...")
        for q in APPLE_SEARCH_QUERIES:
            raw.extend(_safe("apple", search_jobs_apple, q))

        # 3. Anthropic (Greenhouse)
        print("[discovery] Scanning Greenhouse boards (Anthropic)...")
        for slug, name in GREENHOUSE_COMPANIES.items():
            raw.extend(_safe("greenhouse", search_jobs_greenhouse, slug, name))

        # 4. OpenAI (Ashby)
        print("[discovery] Scanning Ashby boards (OpenAI)...")
        for slug, name in ASHBY_COMPANIES.items():
            raw.extend(_safe("ashby", search_jobs_ashby, slug, name))

        # 5. Oracle Careers
        print("[discovery] Scanning Oracle Careers...")
        for q in ORACLE_SEARCH_QUERIES:
            raw.extend(_safe("oracle", search_jobs_oracle, q))

        jobs = deduplicate(raw)
        print(f"[discovery] Total unique relevant jobs found: {len(jobs)}")
        return jobs


def load_queue() -> dict:
    return _load_queue(JOB_QUEUE_PATH)


def save_queue(queue: dict) -> None:
    _save_queue(queue, JOB_QUEUE_PATH)


def add_new_jobs_to_queue(new_jobs: list[dict]) -> int:
    queue = load_queue()
    existing_urls = {j["url"] for j in queue["jobs"] if "url" in j}
    added = 0
    for job in new_jobs:
        # A company/title pair is not a posting identity. The same role may be
        # reopened months later; a new canonical URL should enter the queue.
        if job.get("url") and job.get("url") not in existing_urls:
            job["status"] = "discovered"
            queue["jobs"].append(job)
            existing_urls.add(job["url"])
            added += 1
    save_queue(queue)
    print(f"[discovery] Added {added} new jobs to queue")
    return added


if __name__ == "__main__":
    jobs = discover_jobs()
    add_new_jobs_to_queue(jobs)
