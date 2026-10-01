"""Fetchers for public job-board APIs. Each returns a list of normalized job dicts."""
from __future__ import annotations

import html
import os
import re
import ssl
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TIMEOUT = 30
HEADERS = {"User-Agent": "job-hunter/1.0 (personal job search bot)"}

# Some boards (Job Bank especially) drop connections intermittently.
RETRY = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=None)


class LegacyCipherAdapter(HTTPAdapter):
    """Eluta only offers RSA key exchange (AES128-GCM-SHA256), which Python's defaults leave out.
    Certificate verification is unchanged."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:AES128-GCM-SHA256")
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:AES128-GCM-SHA256")
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(*args, **kwargs)


http = requests.Session()
http.mount("https://", HTTPAdapter(max_retries=RETRY))
http.mount("https://www.eluta.ca/", LegacyCipherAdapter(max_retries=RETRY))


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


def _job(source: dict, job_id, title, location, url, description, posted=None, apply_url=None) -> dict:
    return {
        "id": f"{source['type']}:{source.get('slug') or source.get('keyword')}:{str(job_id).replace(':', '_')}",
        "company": source["company"],
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": url,
        # Straight to the application form where the board exposes one; the posting otherwise.
        "apply_url": apply_url or url,
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
                         j.get("hostedUrl"), "\n\n".join(p for p in parts if p).strip(), posted,
                         j.get("applyUrl") or f"{j.get('hostedUrl')}/apply"))
    return jobs


def fetch_ashby(source: dict) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{source['slug']}"
    r = http.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return [
        _job(source, j["id"], j.get("title"), j.get("location"), j.get("jobUrl"),
             j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")), j.get("publishedAt"),
             j.get("applyUrl") or f"{j.get('jobUrl')}/application")
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


BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
LINKEDIN = "https://www.linkedin.com/jobs-guest/jobs/api"


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return html_to_text(m.group(1)) if m else ""


def linkedin_description(job_id: str) -> str:
    r = http.get(f"{LINKEDIN}/jobPosting/{job_id}", headers=BROWSER_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    body = _first(r'class="show-more-less-html__markup[^"]*">(.*?)</div>', r.text)
    criteria = re.findall(r'description__job-criteria-subheader">\s*(.*?)\s*</h3>.*?'
                          r'description__job-criteria-text[^>]*>\s*(.*?)\s*</span>', r.text, re.S)
    return body + "".join(f"\n{k}: {html_to_text(v)}" for k, v in criteria)


def fetch_linkedin(source: dict) -> list[dict]:
    """LinkedIn's public (logged-out) job search. Newest first, 10 results per page."""
    jobs = {}
    for term in source["searches"]:
        for page in range(source.get("pages", 2)):
            r = http.get(f"{LINKEDIN}/seeMoreJobPostings/search", headers=BROWSER_HEADERS, timeout=TIMEOUT,
                         params={"keywords": term, "location": source.get("location", "Canada"),
                                 "sortBy": "DD", "f_TPR": "r2592000", "start": page * 10})
            r.raise_for_status()
            cards = re.findall(r"<li>(.*?)</li>", r.text, re.S)
            for card in cards:
                job_id = re.search(r'urn:li:jobPosting:(\d+)', card)
                if not job_id or job_id.group(1) in jobs:
                    continue
                job_id = job_id.group(1)
                job = _job(source, job_id, _first(r'base-search-card__title">(.*?)<', card),
                           _first(r'job-search-card__location">(.*?)<', card),
                           f"https://www.linkedin.com/jobs/view/{job_id}", "",
                           (re.search(r'datetime="([^"]+)"', card) or [None, None])[1])
                job["company"] = _first(r'base-search-card__subtitle">(.*?)</h4>', card) or "Unknown employer"
                job["fetch_description"] = lambda job_id=job_id: linkedin_description(job_id)
                jobs[job_id] = job
            time.sleep(1)  # LinkedIn rate-limits aggressively
            if len(cards) < 10:
                break
    return list(jobs.values())


ELUTA = "https://www.eluta.ca"


def eluta_description(path: str, snippet: str) -> str:
    r = http.get(f"{ELUTA}/{path}", headers=BROWSER_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    page = re.sub(r"<!--.*?-->", "", r.text, flags=re.S)
    # Skip <meta itemprop="description"> (the employer blurb); the posting is a <div>/<span>.
    text = _first(r'<(?:div|span|section)[^>]*itemprop="description"[^>]*>(.*?)</(?:div|section)>', page)
    if len(text) >= 200:
        return text
    return (f"(Full posting is only on the employer's site; search snippet follows.)\n{snippet}\n{text}").strip()


def fetch_eluta(source: dict) -> list[dict]:
    """Eluta.ca, a Canadian search engine that crawls employers' own career sites."""
    jobs = {}
    for term in source["searches"]:
        for page in range(1, source.get("pages", 1) + 1):
            r = http.get(f"{ELUTA}/search", params={"q": term, "pg": page},
                         headers=BROWSER_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            blocks = re.split(r'<div data-url="', re.sub(r"<!--.*?-->", "", r.text, flags=re.S))[1:]
            for block in blocks:
                path = block.split('"', 1)[0]
                job_id = re.sub(r"\?.*", "", path).rsplit("-", 1)[-1]
                if job_id in jobs:
                    continue
                job = _job(source, job_id, _first(r'class="lk-job-title"[^>]*>(.*?)</a>', block),
                           _first(r'class="location">(.*?)</span>\s*</span>', block),
                           f"{ELUTA}/{path}", "")
                job["company"] = _first(r'class="employer lk-employer"[^>]*>(.*?)</a>', block) or "Unknown employer"
                snippet = _first(r'class="description">(.*?)</span>\s*(?:<|$)', block)
                job["fetch_description"] = lambda path=path, snippet=snippet: eluta_description(path, snippet)
                jobs[job_id] = job
            if len(blocks) < 10:
                break
    return list(jobs.values())


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "usajobs": fetch_usajobs,
    "jobbank": fetch_jobbank,
    "linkedin": fetch_linkedin,
    "eluta": fetch_eluta,
}


def fetch(source: dict) -> list[dict]:
    from .ats import FETCHERS as ATS_FETCHERS  # ats imports this module

    jobs = {**FETCHERS, **ATS_FETCHERS}[source["type"]](source)
    for job in jobs:
        # Boards that only list jobs in one country declare it in config.yaml.
        if not job.get("country") and source.get("country"):
            job["country"] = source["country"]
    return jobs
