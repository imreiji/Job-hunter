"""Poll job boards, filter, evaluate new postings with DeepSeek, and build the site.

Usage: python -m jobhunter.main [--no-eval] [--config config.yaml]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import yaml

from . import crypto, matcher, sources

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "jobs.json"
APPLICATIONS_FILE = ROOT / "data" / "applications.json"  # written (encrypted) by the tracker's GitHub sync
SALT_FILE = ROOT / "data" / "site_salt.txt"
SITE_DIR = ROOT / "_site"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_profile() -> str | None:
    if os.environ.get("CANDIDATE_PROFILE", "").strip():
        return os.environ["CANDIDATE_PROFILE"]
    path = ROOT / "profile.md"
    return path.read_text() if path.exists() else None


PROVINCES = ("alberta|british columbia|manitoba|new brunswick|newfoundland|labrador|nova scotia|"
             "northwest territories|nunavut|ontario|prince edward island|qu[eé]bec|saskatchewan|yukon")
# Province names in any case, or two-letter codes in capitals ("Toronto, ON", "Victoria (BC)").
CANADA = re.compile(rf"(?i:\bcanada\b|\b(?:{PROVINCES})\b)|\b(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|PEI|QC|SK|YT)\b")
COUNTRY_PATTERNS = {"canada": CANADA}
COUNTRY_ISO = {"canada": "CA"}
LOCAL_FIELDS = ("description", "fetch_description")  # used during a run, never stored


def passes_filters(job: dict, filters: dict) -> bool:
    title, location = job["title"].lower(), job["location"].lower()
    include = [k.lower() for k in filters.get("include_title_keywords") or []]
    exclude = [k.lower() for k in filters.get("exclude_title_keywords") or []]
    locations = [k.lower() for k in filters.get("include_locations") or []]
    if include and not any(k in title for k in include):
        return False
    if any(k in title for k in exclude):
        return False
    if locations and not any(k in location for k in locations):
        return False
    country = (filters.get("country") or "").lower()
    if country:
        if job.get("country"):  # structured country from the board beats parsing the location text
            if job["country"].upper() != COUNTRY_ISO[country]:
                return False
        elif not COUNTRY_PATTERNS[country].search(job["location"]):
            return False
    return True


ROLE_KEYWORDS = [  # first match wins; "Airport Dispatch Agent" is dispatch, "Ramp Crew Chief" is ground
    ("dispatch", ["dispatch", "flight follow", "suivi de vol", "régulateur", "regulateur", "répartiteur",
                  "load control", "socc", "operations control"]),
    ("pilot", ["pilot", "first officer", "captain", "commandant", "premier officier", "co-pilot"]),
]


def classify_role(title: str) -> str:
    t = title.lower()
    for role, words in ROLE_KEYWORDS:
        if any(w in t for w in words):
            return role
    return "ground"


DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y")


def normalize_date(value) -> str | None:
    """Boards report posting dates in a dozen formats; store ISO dates (YYYY-MM-DD) or nothing."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(text).date().isoformat()  # RSS pubDate
    except (TypeError, ValueError):
        return None


def site_password() -> str | None:
    return os.environ.get("SITE_PASSWORD") or None


def tracked_ids(password: str | None) -> set[str]:
    """Jobs in the application tracker are kept even if they later fall outside the filters."""
    try:
        data = json.loads(APPLICATIONS_FILE.read_text())
        if crypto.is_encrypted(data):
            if not password:
                return set()
            data = crypto.decrypt_json(data, password)
        return {a["id"] for a in data.get("applications", []) if a.get("id")}
    except (OSError, ValueError, AttributeError, TypeError) as e:
        if not isinstance(e, FileNotFoundError):
            print(f"! could not read {APPLICATIONS_FILE.name}: {e}")
        return set()


def load_store() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"updated": None, "jobs": []}


COMPANY_NOISE = {"the", "inc", "ltd", "llc", "lp", "corp", "corporation", "limited", "group", "air", "airlines",
                 "airline", "aviation", "aerospace", "canada", "services", "international", "and", "of"}


AGGREGATORS = {"jobbank", "linkedin", "eluta"}


def _words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()  # Québec -> Quebec
    return re.findall(r"[a-z0-9]+", plain.lower())


def posting_key(job: dict) -> tuple[str, str]:
    city = re.split(r"[,(]", re.sub(r"\s+[A-Z]{2}$", "", job["location"].strip()))[0]
    return " ".join(_words(job["title"])), " ".join(_words(city))


def company_tokens(job: dict) -> set[str]:
    return {w for w in _words(job["company"]) if len(w) > 2 and w not in COMPANY_NOISE} or set(_words(job["company"]))


LEGAL_SUFFIXES = {"the", "inc", "ltd", "llc", "lp", "corp", "corporation", "limited", "ltee"}


def name_tokens(company: str) -> set[str]:
    return set(_words(company)) - LEGAL_SUFFIXES


def direct_companies(sources_cfg: list[dict]) -> list[set[str]]:
    return [name_tokens(name) for s in sources_cfg if s["type"] not in AGGREGATORS
            for name in [s["company"], *s.get("aliases", [])]]


def polled_directly(job: dict, companies: list[set[str]]) -> bool:
    """An aggregator copy of a job from an operator whose own board we poll ("Air Canada Rouge"
    covered by "Air Canada"). The operator's board is complete, so the copy adds nothing."""
    if job["id"].split(":", 1)[0] not in AGGREGATORS:
        return False
    tokens = name_tokens(job["company"])
    return any(c and c <= tokens for c in companies)


def find_duplicate(job: dict, index: dict) -> dict | None:
    """The same posting is often on the employer's board, LinkedIn, Eluta and Job Bank."""
    for other in index.get(posting_key(job), []):
        if other["id"].rsplit(":", 1)[0] != job["id"].rsplit(":", 1)[0] and company_tokens(job) & company_tokens(other):
            return other
    return None


def merge(store: dict, fetched: list[dict], polled_prefixes: set[str], ts: str) -> list[dict]:
    """Upsert fetched jobs; mark stored jobs from successfully polled sources as closed if gone.
    A posting already stored from another source is recorded under `also_listed` instead.
    Returns the list of newly seen jobs."""
    by_id = {j["id"]: j for j in store["jobs"]}
    index: dict[tuple, list[dict]] = {}
    for j in store["jobs"]:
        index.setdefault(posting_key(j), []).append(j)
    fetched_ids = set()
    new = []
    for job in fetched:
        fetched_ids.add(job["id"])
        record = {k: v for k, v in job.items() if k not in LOCAL_FIELDS}
        duplicate = None if job["id"] in by_id else find_duplicate(job, index)
        if duplicate:
            listing = {"source": job["id"].split(":", 1)[0], "url": job["url"]}
            if listing not in duplicate.setdefault("also_listed", []):
                duplicate["also_listed"].append(listing)
        elif job["id"] in by_id:
            existing = by_id[job["id"]]
            existing.update(record)
            existing["last_seen"] = ts
            existing["active"] = True
            existing.pop("closed_at", None)
        else:
            record.update(first_seen=ts, last_seen=ts, active=True, evaluation=None)
            by_id[job["id"]] = record
            index.setdefault(posting_key(record), []).append(record)
            new.append(record)
    for job in by_id.values():
        prefix = job["id"].rsplit(":", 1)[0]
        if prefix in polled_prefixes and job["id"] not in fetched_ids and job.get("active"):
            job["active"] = False
            job["closed_at"] = ts
    store["jobs"] = list(by_id.values())
    return new


def source_name(source: dict) -> str:
    return f"{source['type']}:{source.get('slug') or source.get('keyword')}"


def prune(store: dict, configured: set[str], filters: dict, companies: list[set[str]] = (),
          keep: set[str] = frozenset()) -> int:
    """Drop stored jobs whose source was removed from config, that fail the current filters,
    or whose operator is now polled directly. Jobs in `keep` (tracked applications) stay."""
    before = len(store["jobs"])
    store["jobs"] = [j for j in store["jobs"]
                     if j["id"] in keep or (j["id"].rsplit(":", 1)[0] in configured
                                            and passes_filters(j, filters) and not polled_directly(j, companies))]
    return before - len(store["jobs"])


def needs_evaluation(job: dict, profile_hash: str) -> bool:
    ev = job.get("evaluation")
    return job.get("active") and (not ev or ev.get("profile_hash") != profile_hash)


def run(config_path: Path, do_eval: bool, allow_plaintext: bool = False) -> None:
    password = site_password()
    if not password and not allow_plaintext:
        raise SystemExit("SITE_PASSWORD is not set. The site is public, so job data is only published "
                         "encrypted. Set the SITE_PASSWORD secret (or pass --allow-plaintext-site locally).")
    if password and len(password) < crypto.MIN_PASSWORD_LENGTH:
        raise SystemExit(f"SITE_PASSWORD must be at least {crypto.MIN_PASSWORD_LENGTH} characters; "
                         "anyone can download the encrypted file and guess offline.")
    config = yaml.safe_load(config_path.read_text())
    filters = config.get("filters") or {}
    match_cfg = config.get("matching") or {}
    store = load_store()
    ts = now()

    companies = direct_companies(config["sources"])
    removed = prune(store, {source_name(s) for s in config["sources"]}, filters, companies,
                    tracked_ids(password))
    if removed:
        print(f"removed {removed} stored postings no longer covered by config.yaml")

    fetched, polled, descriptions = [], set(), {}
    for source in config["sources"]:
        name = source_name(source)
        try:
            jobs = sources.fetch(source)
        except Exception as e:  # one broken board shouldn't stop the run
            print(f"! {name}: {e}")
            continue
        if source["type"] == "usajobs" and not jobs and not os.environ.get("USAJOBS_API_KEY"):
            continue
        polled.add(name)
        kept = [j for j in jobs if passes_filters(j, filters) and not polled_directly(j, companies)]
        for j in kept:
            j["role"] = classify_role(j["title"])
            j["posted"] = normalize_date(j.get("posted"))
        print(f"{name}: {len(jobs)} postings, {len(kept)} after filters")
        fetched.extend(kept)
        descriptions.update({j["id"]: j for j in kept})

    new = merge(store, fetched, polled, ts)
    for job in store["jobs"]:  # records stored before roles / normalized dates existed
        job.setdefault("role", classify_role(job["title"]))
        if job.get("posted"):
            job["posted"] = normalize_date(job["posted"])
    print(f"{len(new)} new postings")

    profile = load_profile()
    if do_eval and not profile:
        print("! no profile (profile.md or CANDIDATE_PROFILE); skipping evaluation")
    elif do_eval and not os.environ.get("DEEPSEEK_API_KEY"):
        print("! DEEPSEEK_API_KEY not set; skipping evaluation")
    elif do_eval:
        profile_hash = hashlib.sha256(profile.encode()).hexdigest()[:12]
        queue = [j for j in store["jobs"] if needs_evaluation(j, profile_hash) and j["id"] in descriptions]
        queue.sort(key=lambda j: j["first_seen"], reverse=True)
        limit = int(match_cfg.get("max_evaluations_per_run", 40))
        for job in queue[:limit]:
            posting = descriptions[job["id"]]
            try:
                if not posting["description"] and posting.get("fetch_description"):
                    posting["description"] = posting["fetch_description"]()
                result = matcher.evaluate(
                    profile, posting,
                    model=match_cfg.get("model", "deepseek-chat"),
                    max_chars=int(match_cfg.get("max_description_chars", 12000)),
                )
            except Exception as e:
                print(f"! evaluating {job['id']}: {e}")
                continue
            job["evaluation"] = {**result, "profile_hash": profile_hash, "evaluated_at": now()}
            print(f"  {result['verdict']:>10} {result['score']:3d}  {job['company']} - {job['title']}")
        if len(queue) > limit:
            print(f"{len(queue) - limit} postings left pending for later runs")

    store["updated"] = ts
    store["jobs"].sort(key=lambda j: j["first_seen"], reverse=True)
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(store, indent=1, ensure_ascii=False) + "\n")
    build_site(store, password)


def build_site(store: dict, password: str | None) -> None:
    """Static site: the page, its script, and the job data. With a password (always in CI) only
    the encrypted jobs.enc.json is published; plaintext jobs.json is for local development."""
    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)  # never leave a stale plaintext jobs.json behind
    SITE_DIR.mkdir()
    for name in ("index.html", "app.js"):
        shutil.copy(ROOT / "web" / name, SITE_DIR / name)
    if password:
        salt = crypto.load_or_create_salt(SALT_FILE)
        blob = crypto.encrypt_json(store, password, salt)
        (SITE_DIR / "jobs.enc.json").write_text(json.dumps(blob))
    else:
        (SITE_DIR / "jobs.json").write_text(json.dumps(store, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--no-eval", action="store_true", help="poll and store only; skip DeepSeek")
    parser.add_argument("--allow-plaintext-site", action="store_true",
                        help="local development only: build the site with unencrypted jobs.json")
    args = parser.parse_args()
    run(args.config, do_eval=not args.no_eval, allow_plaintext=args.allow_plaintext_site)


if __name__ == "__main__":
    main()
