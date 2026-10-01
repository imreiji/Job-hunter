"""Fetchers for public job-board APIs. Each returns a list of normalized job dicts."""
from __future__ import annotations

import html
import os
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TIMEOUT = 30
HEADERS = {"User-Agent": "job-hunter/1.0 (personal job search bot)"}

# Some boards (Job Bank especially) drop connections intermittently.
http = requests.Session()
http.mount("https://", HTTPAdapter(max_retries=Retry(
    total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=None)))


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)  # Greenhouse double-encodes its HTML
    text = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _job(source: dict, job_id, title, location, url, description, posted=None) -> dict:
    return {
        "id": f"{source['type']}:{source.get('slug') or source.get('keyword')}:{job_id}",
        "company": source["company"],
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": url,
        "description": description,
        "posted": posted,
    }


def fetch_greenhouse(source: dict) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{source['slug']}/jobs?content=true"
    r = http.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [
        _job(source, j["id"], j.get("title"), (j.get("location") or {}).get("name"),
             j.get("absolute_url"), html_to_text(j.get("content")), j.get("updated_at"))
        for j in r.json().get("jobs", [])
    ]


def fetch_lever(source: dict) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{source['slug']}?mode=json"
    r = http.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        parts = [j.get("descriptionPlain", "")]
        for section in j.get("lists", []):
            parts.append(f"{section.get('text', '')}\n{html_to_text(section.get('content'))}")
        parts.append(j.get("additionalPlain", ""))
        created = j.get("createdAt")
        posted = None
        if created:
            from datetime import datetime, timezone
            posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
        jobs.append(_job(source, j["id"], j.get("text"), (j.get("categories") or {}).get("location"),
                         j.get("hostedUrl"), "\n\n".join(p for p in parts if p).strip(), posted))
    return jobs


def fetch_ashby(source: dict) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{source['slug']}"
    r = http.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [
        _job(source, j["id"], j.get("title"), j.get("location"), j.get("jobUrl"),
             j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")), j.get("publishedAt"))
        for j in r.json().get("jobs", [])
        if j.get("isListed", True)
    ]


def fetch_usajobs(source: dict) -> list[dict]:
    key, email = os.environ.get("USAJOBS_API_KEY"), os.environ.get("USAJOBS_EMAIL")
    if not key or not email:
        print("  skipping USAJOBS: USAJOBS_API_KEY / USAJOBS_EMAIL not set")
        return []
    r = http.get(
        "https://data.usajobs.gov/api/search",
        params={"Keyword": source["keyword"], "ResultsPerPage": 250},
        headers={"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    jobs = []
    for item in r.json().get("SearchResult", {}).get("SearchResultItems", []):
        d = item["MatchedObjectDescriptor"]
        details = (d.get("UserArea") or {}).get("Details") or {}
        desc = "\n\n".join(filter(None, [
            d.get("QualificationSummary"),
            details.get("JobSummary"),
            "\n".join(details.get("MajorDuties") or []),
            details.get("Requirements"),
        ]))
        org = d.get("OrganizationName") or "USAJOBS"
        job = _job(source, d["PositionID"], d.get("PositionTitle"), d.get("PositionLocationDisplay"),
                   d.get("PositionURI"), desc, d.get("PublicationStartDate"))
        job["company"] = org
        jobs.append(job)
    return jobs


JOBBANK = "https://www.jobbank.gc.ca"


def _jobbank_field(article: str, cls: str) -> str:
    m = re.search(rf'class="{cls}">(.*?)</(?:li|h3)>', article, re.S)
    return html_to_text(re.sub(r'<span class="wb-inv">.*?</span>', "", m.group(1), flags=re.S)) if m else ""


def jobbank_description(job_id: str) -> str:
    r = http.get(f"{JOBBANK}/jobsearch/jobposting/{job_id}", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    m = re.search(r"<main.*?</main>", r.text, re.S)
    text = html_to_text(re.sub(r"<(script|style).*?</\1>", "", m.group(0) if m else r.text, flags=re.S))
    start = text.find("Job details")
    return text[start:] if start >= 0 else text


def fetch_jobbank(source: dict) -> list[dict]:
    """Government of Canada Job Bank keyword searches (also aggregates many other boards).
    Only the first page of each search (newest ~25) is read."""
    jobs = {}
    for term in source["searches"]:
        r = http.get(f"{JOBBANK}/jobsearch/jobsearch", params={"searchstring": term, "sort": "D"},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        for job_id, article in re.findall(r'<article id="article-(\d+)"[^>]*>(.*?)</article>', r.text, re.S):
            if job_id in jobs:
                continue
            job = _job(source, job_id, _jobbank_field(article, "noctitle"), _jobbank_field(article, "location"),
                       f"{JOBBANK}/jobsearch/jobposting/{job_id}", "", None)
            job["company"] = _jobbank_field(article, "business") or "Unknown employer"
            job["fetch_description"] = lambda job_id=job_id: jobbank_description(job_id)
            jobs[job_id] = job
    return list(jobs.values())


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "usajobs": fetch_usajobs,
    "jobbank": fetch_jobbank,
}


def fetch(source: dict) -> list[dict]:
    return FETCHERS[source["type"]](source)
