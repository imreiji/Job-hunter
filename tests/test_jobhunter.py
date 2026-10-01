import json

import pytest

from jobhunter import main, matcher, sources


def job(id_, title="Pilot", location="Denver, CO"):
    return {"id": id_, "company": "Acme", "title": title, "location": location,
            "url": "https://x", "description": "desc", "posted": None}


def test_html_to_text_handles_greenhouse_double_encoding():
    raw = "&lt;p&gt;Hold an &lt;strong&gt;ATP&lt;/strong&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;1500 hrs&lt;/li&gt;&lt;/ul&gt;"
    assert sources.html_to_text(raw) == "Hold an ATP\n- 1500 hrs"


@pytest.mark.parametrize("filters,title,location,expected", [
    ({}, "Avionics Tech", "TX", True),
    ({"include_title_keywords": ["pilot"]}, "First Officer", "TX", False),
    ({"include_title_keywords": ["pilot"]}, "Test Pilot", "TX", True),
    ({"exclude_title_keywords": ["intern"]}, "Flight Test Intern", "TX", False),
    ({"include_locations": ["remote", "colorado"]}, "Pilot", "Denver, Colorado", True),
    ({"include_locations": ["remote"]}, "Pilot", "Denver, Colorado", False),
])
def test_passes_filters(filters, title, location, expected):
    assert main.passes_filters(job("a", title, location), filters) is expected


def test_merge_tracks_new_and_closed_jobs():
    store = {"jobs": [
        {**job("greenhouse:acme:1"), "first_seen": "t0", "active": True, "evaluation": {"score": 5}},
        {**job("greenhouse:acme:2"), "first_seen": "t0", "active": True},
        {**job("lever:other:9", title="Dispatcher"), "first_seen": "t0", "active": True},  # source failed
    ]}
    fetched = [job("greenhouse:acme:1"), job("greenhouse:acme:3", title="Captain")]
    new = main.merge(store, fetched, {"greenhouse:acme"}, "t1")

    by_id = {j["id"]: j for j in store["jobs"]}
    assert [j["id"] for j in new] == ["greenhouse:acme:3"]
    assert by_id["greenhouse:acme:1"]["evaluation"] == {"score": 5}  # kept
    assert by_id["greenhouse:acme:1"]["last_seen"] == "t1"
    assert by_id["greenhouse:acme:2"]["active"] is False
    assert by_id["greenhouse:acme:2"]["closed_at"] == "t1"
    assert by_id["lever:other:9"]["active"] is True  # not polled, so not closed
    assert "description" not in by_id["greenhouse:acme:3"]


def test_needs_evaluation_when_profile_changes():
    j = {"active": True, "evaluation": {"profile_hash": "old"}}
    assert main.needs_evaluation(j, "new")
    assert not main.needs_evaluation(j, "old")
    assert not main.needs_evaluation({"active": False, "evaluation": None}, "new")


def test_parse_result_normalizes_output():
    raw = "```json\n" + json.dumps({"score": 140, "verdict": "Strong", "summary": "ok",
                                    "met": ["ATP"], "missing": "n/a"}) + "\n```"
    r = matcher.parse_result(raw)
    assert r["score"] == 100 and r["verdict"] == "strong"
    assert r["met"] == ["ATP"] and r["missing"] == [] and r["dealbreakers"] == []


@pytest.mark.parametrize("raw", ["not json", '{"score": 50, "verdict": "maybe"}'])
def test_parse_result_rejects_bad_output(raw):
    with pytest.raises(matcher.MatchError):
        matcher.parse_result(raw)


JOBBANK_HTML = """<html><article id="article-50232102" class="action-buttons"><a href="/x" class="resultJobItem">
  <h3 class="title"><span class="flag"></span>
    <span class="noctitle"> ramp agent - air transport
    </span></h3>
  <ul class="list-unstyled"><li class="date">September 06, 2026</li>
    <li class="business">Jazz Aviation LP</li>
    <li class="location"><span class="fas fa-map-marker-alt" aria-hidden="true"></span> <span class="wb-inv">Location</span>
        Victoria (BC)
    </li></ul></a></article></html>"""


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_jobbank_parses_search_results_and_dedupes(monkeypatch):
    monkeypatch.setattr(sources.http, "get", lambda *a, **kw: FakeResponse(JOBBANK_HTML))
    jobs = sources.fetch_jobbank({"type": "jobbank", "slug": "jobbank", "company": "Job Bank",
                                  "searches": ["ramp agent", "baggage handler"]})
    assert len(jobs) == 1  # same posting returned by both searches
    j = jobs[0]
    assert (j["id"], j["company"], j["title"], j["location"]) == (
        "jobbank:jobbank:50232102", "Jazz Aviation LP", "ramp agent - air transport", "Victoria (BC)")
    assert j["url"].endswith("/jobposting/50232102")


@pytest.mark.parametrize("location,expected", [
    ("Toronto, ON", True), ("Victoria (BC)", True), ("Montréal, Québec, Canada", True),
    ("Remote - Canada", True), ("Hawthorne, CA", False), ("Seattle, WA", False), ("", False),
])
def test_country_filter(location, expected):
    assert main.passes_filters(job("a", location=location), {"country": "canada"}) is expected


def test_merge_never_stores_run_only_fields():
    store = {"jobs": []}
    main.merge(store, [{**job("jobbank:jobbank:1"), "fetch_description": lambda: "x"}], set(), "t")
    assert set(main.LOCAL_FIELDS).isdisjoint(store["jobs"][0])


def test_prune_drops_removed_sources_and_filtered_jobs():
    store = {"jobs": [job("greenhouse:spacex:1"), job("jobbank:jobbank:2", title="Pilot"),
                      job("jobbank:jobbank:3", title="Cashier")]}
    removed = main.prune(store, {"jobbank:jobbank"}, {"include_title_keywords": ["pilot"]})
    assert removed == 2 and [j["id"] for j in store["jobs"]] == ["jobbank:jobbank:2"]


def test_merge_folds_same_posting_from_another_source():
    store = {"jobs": []}
    a = {**job("linkedin:linkedin:1", "Ramp Agent", "Moncton, New Brunswick, Canada"), "company": "Air Canada"}
    b = {**job("eluta:eluta:x", "Ramp Agent", "Moncton NB"), "company": "Air Canada Inc."}
    c = {**job("eluta:eluta:y", "Ramp Agent", "Moncton NB"), "company": "Swissport"}  # different employer
    d = {**job("linkedin:linkedin:2", "Ramp Agent", "Moncton, New Brunswick, Canada"), "company": "Air Canada"}
    new = main.merge(store, [a, b, c, d], {"linkedin:linkedin", "eluta:eluta"}, "t")
    assert [j["id"] for j in new] == ["linkedin:linkedin:1", "eluta:eluta:y", "linkedin:linkedin:2"]
    first = next(j for j in store["jobs"] if j["id"] == "linkedin:linkedin:1")
    assert first["also_listed"] == [{"source": "eluta", "url": "https://x"}]
