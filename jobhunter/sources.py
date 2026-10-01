"""Fetchers for public job-board APIs. Each returns a list of normalized job dicts."""
from __future__ import annotations

import html
import os
import re

import requests

TIMEOUT = 30
HEADERS = {"User-Agent": "job-hunter/1.0 (personal job search bot)"}


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
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [
        _job(source, j["id"], j.get("title"), (j.get("location") or {}).get("name"),
             j.get("absolute_url"), html_to_text(j.get("content")), j.get("updated_at"))
        for j in r.json().get("jobs", [])
    ]


def fetch_lever(source: dict) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{source['slug']}?mode=json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
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
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
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
    r = requests.get(
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


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "usajobs": fetch_usajobs,
}


def fetch(source: dict) -> list[dict]:
    return FETCHERS[source["type"]](source)
