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
        {**job("lever:other:9"), "first_seen": "t0", "active": True},  # its source failed this run
    ]}
    fetched = [job("greenhouse:acme:1"), job("greenhouse:acme:3")]
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
