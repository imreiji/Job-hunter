"""Fetchers for the applicant-tracking systems Canadian operators use.

Each fetcher takes a source dict from config.yaml and returns normalized jobs (see sources._job).
Jobs whose description needs a second request carry a `fetch_description` callable instead,
so detail pages are only loaded for postings that are about to be evaluated.
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote

import requests

from .sources import BROWSER_HEADERS, RETRY, TIMEOUT, HTTPAdapter, _job, html_to_text, http

COUNTRY_CODES = {"CAN": "CA", "USA": "US", "CANADA": "CA", "UNITED STATES": "US"}


def country_code(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().upper()
    return COUNTRY_CODES.get(value, value if len(value) == 2 else None)


def _get(url: str, **kw) -> requests.Response:
    r = http.get(url, headers={**BROWSER_HEADERS, **kw.pop("headers", {})}, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _post(url: str, body: dict, **kw) -> requests.Response:
    r = http.post(url, json=body, headers={**BROWSER_HEADERS, **kw.pop("headers", {})}, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _place(*parts: str | None) -> str:
    return ", ".join(p.strip() for p in parts if p and p.strip())


# --- Phenom (Air Canada) --------------------------------------------------------------------

def fetch_phenom(source: dict) -> list[dict]:
    host = source["host"]
    base = {"lang": source.get("lang", "en_ca"), "deviceType": "desktop", "country": "ca", "siteType": "external"}
    jobs, start = [], 0
    while True:
        body = {**base, "pageName": "search-results", "ddoKey": "refineSearch", "from": start, "size": 50,
                "jobs": True, "counts": True, "all_fields": ["category", "country", "state", "city", "type"],
                "pageId": "page20", "keywords": "", "global": True, "locationData": {},
                "selected_fields": {"country": [source.get("phenom_country", "CANADA")]}}
        data = _post(f"https://{host}/widgets", body).json()["refineSearch"]
        page = data["data"]["jobs"]
        for j in page:
            seq = j["jobSeqNo"]
            job = _job(source, seq, j.get("title"), _place(j.get("city", "").title(), j.get("state", "").title()),
                       f"https://{host}/ca/en/job/{j.get('jobId', seq)}", "", j.get("postedDate"))
            job["country"] = country_code(j.get("country"))
            job["fetch_description"] = lambda seq=seq: html_to_text(_post(
                f"https://{host}/widgets", {**base, "pageName": "job", "ddoKey": "jobDetail", "jobSeqNo": seq}
            ).json()["jobDetail"]["data"]["job"]["description"])
            jobs.append(job)
        start += len(page)
        if not page or start >= data["totalHits"]:
            return jobs


# --- Taleo career section REST (Jazz) -------------------------------------------------------

TALEO_FILTERS = ["ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER", "URGENT_JOB", "EMPLOYEE_STATUS",
                 "STUDY_LEVEL", "WILL_TRAVEL", "JOB_SHIFT"]


def _taleo_location(raw: str) -> tuple[str, str | None]:
    """'["CA-QC-Montréal"]' -> ('Montréal, QC', 'CA')"""
    try:
        first = (json.loads(raw) or [""])[0]
    except (ValueError, TypeError):
        first = raw or ""
    parts = first.split("-", 2)
    if len(parts) == 3:
        return f"{parts[2]}, {parts[1]}", parts[0]
    return first, None


def taleo_description(host: str, section: str, contest_no: str) -> str:
    page = _get(f"https://{host}/careersection/{section}/jobdetail.ftl", params={"job": contest_no, "lang": "en"}).text
    m = re.search(r'id="initialHistory"[^>]*value="([^"]*)"', page)
    if not m:
        return html_to_text(page)
    # The value is a '!|!'-separated list of URL-encoded fields; the HTML ones hold the posting.
    fields = [unquote(f) for f in unquote(m.group(1)).split("!|!")]
    text = "\n\n".join(html_to_text(f) for f in fields if "<" in f and len(f) > 80)
    return text.replace("!*!", "").strip()


def fetch_taleo(source: dict) -> list[dict]:
    host, section = source["host"], source.get("section", "2")
    body = {"multilineEnabled": False,
            "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
            "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
            "filterSelectionParam": {"searchFilterSelections": [
                {"id": i, "selectedValues": []} for i in ("POSTING_DATE", "LOCATION", "JOB_FIELD")]},
            "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [
                {"id": i, "selectedValues": []} for i in TALEO_FILTERS]},
            "pageNo": 1}
    headers = {"tz": "GMT-04:00", "tzname": "America/Toronto"}
    jobs = []
    while True:
        data = _post(f"https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={source['portal']}",
                     body, headers=headers).json()
        for r in data["requisitionList"]:
            title, raw_location, posted = (r["column"] + ["", "", ""])[:3]
            location, country = _taleo_location(raw_location)
            no = r["contestNo"]
            job = _job(source, no, title, location,
                       f"https://{host}/careersection/{section}/jobdetail.ftl?job={no}&lang=en", "", posted)
            job["country"] = country
            job["fetch_description"] = lambda no=no: taleo_description(host, section, no)
            jobs.append(job)
        paging = data["pagingData"]
        if not data["requisitionList"] or paging["currentPageNo"] * paging["pageSize"] >= paging["totalCount"]:
            return jobs
        body["pageNo"] += 1


# --- UKG / UltiPro (Cargojet, Canadian North, Voyageur, Menzies) ----------------------------

def ukg_description(board: str, opportunity_id: str) -> str:
    page = _get(f"{board}/OpportunityDetail", params={"opportunityId": opportunity_id}).text
    m = re.search(r'"Description":("(?:[^"\\]|\\.)*")', page)
    return html_to_text(json.loads(m.group(1))) if m else ""


def fetch_ukg(source: dict) -> list[dict]:
    board = f"https://{source['host']}/{source['tenant']}/JobBoard/{source['board']}"
    body = {"opportunitySearch": {"Top": 100, "Skip": 0, "QueryString": "", "Filters": [],
                                  "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate",
                                               "Ascending": False}]},
            "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [], "Skills": [],
                              "hasNoLicenses": False, "SkippedSkills": []}}
    jobs = []
    while True:
        data = _post(f"{board}/JobBoardView/LoadSearchResults", body).json()
        for o in data["opportunities"]:
            addresses = [loc.get("Address") or {} for loc in o.get("Locations") or []]
            first = addresses[0] if addresses else {}
            job = _job(source, o["Id"], o.get("Title"),
                       _place(first.get("City"), (first.get("State") or {}).get("Code")),
                       f"{board}/OpportunityDetail?opportunityId={o['Id']}", "", o.get("PostedDate"))
            countries = {country_code((a.get("Country") or {}).get("Code")) for a in addresses} - {None}
            job["country"] = "CA" if "CA" in countries else next(iter(countries), None)
            job["fetch_description"] = lambda oid=o["Id"]: ukg_description(board, oid)
            jobs.append(job)
        body["opportunitySearch"]["Skip"] += 100
        if not data["opportunities"] or body["opportunitySearch"]["Skip"] >= data["totalCount"]:
            return jobs


# --- ADP Workforce Now (PAL group, Harbour Air, KF Aerospace) -------------------------------

ADP = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"


def _adp_field(req: dict, code: str) -> str | None:
    for f in (req.get("customFieldGroup") or {}).get("stringFields", []):
        if f.get("nameCode", {}).get("codeValue") == code:
            return f.get("stringValue")
    return None


def fetch_adp(source: dict) -> list[dict]:
    params = {"cid": source["cid"], "ccId": source["ccid"], "lang": "en_CA", "locale": "en_CA"}
    jobs, seen, skip = [], set(), 0
    while True:
        data = _get(ADP, params={**params, "timeStamp": int(time.time() * 1000), "$top": 20, "$skip": skip}).json()
        page = data.get("jobRequisitions", [])
        for req in page:
            item = req["itemID"]
            if item in seen:
                continue
            seen.add(item)
            loc = (req.get("requisitionLocations") or [{}])[0]
            addr = loc.get("address") or {}
            location = _place(addr.get("cityName"), (addr.get("countrySubdivisionLevel1") or {}).get("codeValue")) \
                or (loc.get("nameCode") or {}).get("shortName", "")
            job_id = _adp_field(req, "ExternalJobID") or item
            job = _job(source, item, req.get("requisitionTitle"), location,
                       "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
                       f"?cid={source['cid']}&ccId={source['ccid']}&jobId={job_id}&lang=en_CA", "", req.get("postDate"))
            job["fetch_description"] = lambda item=item: html_to_text(
                _get(f"{ADP}/{item}", params=params).json().get("requisitionDescription"))
            jobs.append(job)
        skip += 20
        if not page or skip >= (data.get("meta") or {}).get("totalNumber", 0):
            return jobs


# --- Dayforce (WestJet, Pacific Coastal, Perimeter, Calm Air, Keewatin, Conair, GAT...) ------

DAYFORCE = "https://jobs.dayforcehcm.com"


def fetch_dayforce(source: dict) -> list[dict]:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=RETRY))
    session.headers.update(BROWSER_HEADERS)
    token = session.get(f"{DAYFORCE}/api/auth/csrf", timeout=TIMEOUT).json()["csrfToken"]
    ns = source["namespace"]
    jobs, seen_reqs = [], set()
    for board in source["boards"]:
        start = 0
        while True:
            r = session.post(f"{DAYFORCE}/api/geo/{ns}/jobposting/search", headers={"X-CSRF-TOKEN": token},
                             timeout=TIMEOUT, json={"clientNamespace": ns, "jobBoardCode": board, "cultureCode": "en-US",
                                                    "distanceUnit": 0, "paginationStart": start})
            r.raise_for_status()
            data = r.json()
            for p in data["jobPostings"]:
                if p["jobReqId"] in seen_reqs:  # the same requisition is often posted on several boards
                    continue
                seen_reqs.add(p["jobReqId"])
                locs = p.get("postingLocations") or [{}]
                job = _job(source, p["jobReqId"], p.get("jobTitle"),
                           _place(locs[0].get("cityName"), locs[0].get("stateCode")),
                           f"{DAYFORCE}/en-US/{ns}/{board}/jobs/{p['jobPostingId']}",
                           html_to_text(p.get("jobDescription")), p.get("postingStartTimestampUTC"))
                countries = {l.get("isoCountryCode") for l in locs} - {None}
                job["country"] = "CA" if "CA" in countries else next(iter(countries), None)
                jobs.append(job)
            start += 25
            if not data["jobPostings"] or start >= data["maxCount"]:
                break
    return jobs


# --- iCIMS career sites / Jibe (Porter, Swissport) ------------------------------------------

def fetch_jibe(source: dict) -> list[dict]:
    jobs, page = [], 1
    while True:
        data = _get(f"https://{source['host']}/api/jobs",
                    params={"page": page, "limit": 100, **source.get("params", {})}).json()
        for item in data["jobs"]:
            j = item["data"]
            description = "\n\n".join(html_to_text(j.get(k)) for k in ("description", "responsibilities",
                                                                        "qualifications") if j.get(k))
            job = _job(source, j["req_id"], j.get("title"), _place(j.get("city"), j.get("state")),
                       f"https://{source['host']}/jobs/{j['slug']}?lang={j.get('language', 'en-us')}",
                       description, j.get("posted_date"))
            job["country"] = country_code(j.get("country_code"))
            jobs.append(job)
        if not data["jobs"] or page * 100 >= data.get("totalCount", 0):
            return jobs
        page += 1


# --- SmartRecruiters (Air Transat, Canadian Helicopters, Kenn Borek) ------------------------

SMARTRECRUITERS = "https://api.smartrecruiters.com/v1/companies"


def smartrecruiters_description(company: str, posting_id: str) -> str:
    sections = _get(f"{SMARTRECRUITERS}/{company}/postings/{posting_id}").json().get("jobAd", {}).get("sections", {})
    return "\n\n".join(f"{s.get('title', '')}\n{html_to_text(s.get('text'))}" for s in sections.values()
                       if isinstance(s, dict) and s.get("text"))


def fetch_smartrecruiters(source: dict) -> list[dict]:
    company, jobs, offset = source["company_id"], [], 0
    while True:
        data = _get(f"{SMARTRECRUITERS}/{company}/postings", params={"limit": 100, "offset": offset}).json()
        for p in data["content"]:
            loc = p.get("location") or {}
            job = _job(source, p["id"], p.get("name"), _place(loc.get("city"), loc.get("region")),
                       f"https://jobs.smartrecruiters.com/{company}/{p['id']}", "", p.get("releasedDate"))
            job["country"] = country_code(loc.get("country"))
            job["fetch_description"] = lambda pid=p["id"]: smartrecruiters_description(company, pid)
            jobs.append(job)
        offset += 100
        if not data["content"] or offset >= data["totalFound"]:
            return jobs


# --- BambooHR (Air Tindi, Morningstar) ------------------------------------------------------

def fetch_bamboohr(source: dict) -> list[dict]:
    base = f"https://{source['slug']}.bamboohr.com/careers"
    jobs = []
    for j in _get(f"{base}/list", headers={"Accept": "application/json"}).json()["result"]:
        loc = j.get("location") or {}
        job = _job(source, j["id"], j.get("jobOpeningName"), _place(loc.get("city"), loc.get("state")),
                   f"{base}/{j['id']}", "")
        job["fetch_description"] = lambda jid=j["id"]: html_to_text(
            _get(f"{base}/{jid}/detail", headers={"Accept": "application/json"}).json()
            ["result"]["jobOpening"].get("description"))
        jobs.append(job)
    return jobs


# --- Rippling (Rise Air) --------------------------------------------------------------------

def fetch_rippling(source: dict) -> list[dict]:
    base = f"https://ats.rippling.com/api/v2/board/{source['slug']}/jobs"
    jobs, page = [], 0
    while True:
        data = _get(base, params={"page": page, "pageSize": 50}).json()
        for j in data["items"]:
            loc = (j.get("locations") or [{}])[0]
            job = _job(source, j["id"], j.get("name"), _place(loc.get("city"), loc.get("stateCode")), j.get("url"), "")
            job["country"] = country_code(loc.get("countryCode"))

            def describe(jid=j["id"]):
                d = _get(f"{base}/{jid}").json().get("description")
                if isinstance(d, dict):
                    d = "\n".join(str(v) for v in d.values() if v)
                return html_to_text(d)
            job["fetch_description"] = describe
            jobs.append(job)
        page += 1
        if not data["items"] or page >= data.get("totalPages", 1):
            return jobs


# --- Squarespace collection (Central Mountain Air) ------------------------------------------

def fetch_squarespace(source: dict) -> list[dict]:
    site = source["site"].rstrip("/")
    jobs = []
    for item in _get(f"{site}/{source['collection']}", params={"format": "json"}).json().get("items", []):
        title = item.get("title", "")
        # Titles usually end with the base: "Captain - Smithers, BC (YYD)".
        location = title.rsplit(" - ", 1)[1] if " - " in title else ""
        jobs.append(_job(source, item["id"], title, location, f"{site}{item['fullUrl']}",
                         html_to_text(item.get("body")), None))
    return jobs


# --- RSS (Wasaya, WordPress career post types) ----------------------------------------------

def fetch_rss(source: dict) -> list[dict]:
    root = ET.fromstring(_get(source["url"]).content)
    content = "{http://purl.org/rss/1.0/modules/content/}encoded"
    jobs = []
    for item in root.iter("item"):
        title = item.findtext("title", "")
        link = item.findtext("link", "")
        body = item.findtext(content) or item.findtext("description", "")
        m = re.search(r"\(([A-Z]{3})\)", title)  # airport code in the title, e.g. "Ramp Attendant (YQT)"
        jobs.append(_job(source, item.findtext("guid") or link, title, m.group(1) if m else "", link,
                         html_to_text(body), item.findtext("pubDate")))
    return jobs


FETCHERS = {
    "phenom": fetch_phenom,
    "taleo": fetch_taleo,
    "ukg": fetch_ukg,
    "adp": fetch_adp,
    "dayforce": fetch_dayforce,
    "jibe": fetch_jibe,
    "smartrecruiters": fetch_smartrecruiters,
    "bamboohr": fetch_bamboohr,
    "rippling": fetch_rippling,
    "squarespace": fetch_squarespace,
    "rss": fetch_rss,
}
