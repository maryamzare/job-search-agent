"""
Module 1 — Job Discovery
Scrapes job postings matching Marmar's target roles and filters.

Sources:
  1. LinkedIn guest jobs API  — no login, public endpoint
  2. Greenhouse ATS API       — public JSON API for companies on Greenhouse
  3. Lever ATS API            — public JSON API for companies on Lever
"""

import json
import time
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
from config import TARGET_ROLES, TARGET_LOCATIONS, JOB_QUEUE_PATH
from modules.util import load_queue as _load_queue, save_queue as _save_queue, track_stage

# Companies to query directly via ATS APIs (slug: display name)
GREENHOUSE_COMPANIES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "scale": "Scale AI",
    "figma": "Figma",
    "notion": "Notion",
    "stripe": "Stripe",
    "databricks": "Databricks",
    "cohere": "Cohere",
    "mistral": "Mistral AI",
    "perplexity": "Perplexity",
}

LEVER_COMPANIES = {
    "netflix": "Netflix",
    "canva": "Canva",
    "airtable": "Airtable",
    "vercel": "Vercel",
}

# Keywords that must appear in the job title to be considered relevant
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


# ── LinkedIn ──────────────────────────────────────────────────────────────────

def _fetch_linkedin_page(role: str, location: str, start: int) -> list[dict]:
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote_plus(role)}&location={quote_plus(location)}&start={start}"
    )
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
    # Extract job ID from URL e.g. .../view/1234567890/
    match = re.search(r"/view/(\d+)", job_url)
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
        # Fallback: grab the description section
        desc_el = soup.find("div", {"class": re.compile(r"description")})
        return desc_el.get_text(separator="\n", strip=True) if desc_el else ""
    except requests.RequestException:
        return ""


def search_jobs_linkedin(role: str, location: str, pages: int = 2) -> list[dict]:
    print(f"[discovery] LinkedIn: '{role}' in '{location}'")
    jobs = []
    for page in range(pages):
        batch = _fetch_linkedin_page(role, location, start=page * 25)
        jobs.extend(batch)
        if not batch:
            break
        time.sleep(1.5)
    print(f"  → {len(jobs)} relevant listings")
    return jobs


# ── Greenhouse ────────────────────────────────────────────────────────────────

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
        content = BeautifulSoup(item.get("content", ""), "html.parser").get_text(
            separator="\n", strip=True
        )
        jobs.append({
            "title": title,
            "company": company_name,
            "location": location,
            "url": item.get("absolute_url", ""),
            "apply_url": item.get("absolute_url", ""),
            "description": content,
        })
    print(f"  [greenhouse] {company_name}: {len(jobs)} relevant listings")
    return jobs


# ── Lever ─────────────────────────────────────────────────────────────────────

def search_jobs_lever(company_slug: str, company_name: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"  [lever] {company_name}: HTTP {resp.status_code}")
            return []
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  [lever] {company_name}: {e}")
        return []

    jobs = []
    for item in data:
        title = item.get("text", "")
        if not _title_matches(title):
            continue
        categories = item.get("categories", {})
        location = categories.get("location", categories.get("team", ""))
        if location and not _location_matches(location):
            continue
        description_parts = []
        for section in item.get("descriptionBody", {}).get("body", []):
            if section.get("text"):
                description_parts.append(section["text"])
        description = "\n".join(description_parts).strip()
        apply_url = item.get("applyUrl", item.get("hostedUrl", ""))
        jobs.append({
            "title": title,
            "company": company_name,
            "location": location,
            "url": item.get("hostedUrl", apply_url),
            "apply_url": apply_url,
            "description": description,
        })
    print(f"  [lever] {company_name}: {len(jobs)} relevant listings")
    return jobs


# ── Helpers ───────────────────────────────────────────────────────────────────

def _title_matches(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in TITLE_KEYWORDS)


def _location_matches(location: str) -> bool:
    if not location:
        return True  # no location = assume remote/flexible
    loc = location.lower()
    return any(
        kw in loc
        for kw in ["seattle", "remote", "hybrid", "bellevue", "kirkland", "redmond", "nationwide", "united states", "usa", "us"]
    )


def deduplicate(jobs: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for job in jobs:
        key = (job.get("company", "").lower(), job.get("title", "").lower())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


# ── Orchestration ─────────────────────────────────────────────────────────────

def discover_jobs() -> list[dict]:
    with track_stage("module1_discovery"):
        raw = []

        # LinkedIn search
        for role in TARGET_ROLES:
            for location in TARGET_LOCATIONS:
                if location == "Hybrid":
                    continue  # LinkedIn doesn't have a "Hybrid" location filter
                raw.extend(search_jobs_linkedin(role, location))
                time.sleep(2)

        # ATS APIs
        print("[discovery] Scanning Greenhouse company boards...")
        for slug, name in GREENHOUSE_COMPANIES.items():
            raw.extend(search_jobs_greenhouse(slug, name))

        print("[discovery] Scanning Lever company boards...")
        for slug, name in LEVER_COMPANIES.items():
            raw.extend(search_jobs_lever(slug, name))

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
    existing_keys = {
        (j.get("company", "").lower(), j.get("title", "").lower())
        for j in queue["jobs"]
    }
    added = 0
    for job in new_jobs:
        key = (job.get("company", "").lower(), job.get("title", "").lower())
        if job.get("url") not in existing_urls and key not in existing_keys:
            job["status"] = "discovered"
            queue["jobs"].append(job)
            added += 1
    save_queue(queue)
    print(f"[discovery] Added {added} new jobs to queue")
    return added


if __name__ == "__main__":
    jobs = discover_jobs()
    add_new_jobs_to_queue(jobs)
