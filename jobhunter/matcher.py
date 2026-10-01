"""Asks DeepSeek whether the candidate meets a job's requirements."""
from __future__ import annotations

import json
import os

import requests

API_URL = "https://api.deepseek.com/chat/completions"
VERDICTS = ("strong", "possible", "weak", "ineligible")

SYSTEM_PROMPT = """You are a meticulous aviation-industry recruiter screening job postings for one candidate.
Compare the candidate profile against the job's stated requirements. Pay particular attention to
hard gates common in aviation: FAA certificates and ratings (ATP, Commercial, CFI, A&P, IA, dispatcher),
flight hours (total, PIC, multi-engine, turbine), type ratings, medical class, degrees, years of experience,
security clearances, and citizenship / ITAR / export-control requirements.

Rules:
- Only use facts in the candidate profile. If the profile does not mention something the job requires, treat it as missing.
- Distinguish hard requirements ("required", "must") from preferred qualifications.
- verdict is one of:
  "strong"     - meets all hard requirements and most preferred ones
  "possible"   - meets most hard requirements; gaps are small or arguable
  "weak"       - misses several hard requirements
  "ineligible" - fails a non-negotiable gate (certificate, hours, clearance, citizenship, medical, license)
- score is 0-100 and must be consistent with the verdict.

Respond with a single JSON object, no prose, with exactly these keys:
{"score": int, "verdict": str, "summary": str (2-3 sentences, second person, addressed to the candidate),
 "met": [str], "missing": [str], "dealbreakers": [str]}"""


class MatchError(RuntimeError):
    pass


def build_user_prompt(profile: str, job: dict, max_chars: int) -> str:
    return (
        f"## Candidate profile\n{profile.strip()}\n\n"
        f"## Job posting\nTitle: {job['title']}\nCompany: {job['company']}\n"
        f"Location: {job['location']}\n\n{job['description'][:max_chars]}"
    )


def parse_result(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise MatchError(f"model returned invalid JSON: {content[:200]!r}") from e
    verdict = str(data.get("verdict", "")).lower()
    if verdict not in VERDICTS:
        raise MatchError(f"unexpected verdict {verdict!r}")
    try:
        score = max(0, min(100, int(data.get("score", 0))))
    except (TypeError, ValueError) as e:
        raise MatchError(f"bad score {data.get('score')!r}") from e
    as_list = lambda v: [str(x) for x in v] if isinstance(v, list) else []
    return {
        "score": score,
        "verdict": verdict,
        "summary": str(data.get("summary", "")),
        "met": as_list(data.get("met")),
        "missing": as_list(data.get("missing")),
        "dealbreakers": as_list(data.get("dealbreakers")),
    }


def evaluate(profile: str, job: dict, model: str, max_chars: int) -> dict:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise MatchError("DEEPSEEK_API_KEY not set")
    r = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(profile, job, max_chars)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        },
        timeout=120,
    )
    if r.status_code != 200:
        raise MatchError(f"DeepSeek HTTP {r.status_code}: {r.text[:200]}")
    return parse_result(r.json()["choices"][0]["message"]["content"])
