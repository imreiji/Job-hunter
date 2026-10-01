# Job Hunter ✈️

Polls Canadian aviation job boards every few hours for pilot, flight dispatcher, and ramp / ground-handling roles. DeepSeek screens each new posting
against your profile, and every posting goes on a filterable webpage hosted on GitHub Pages.

```
GitHub Actions (cron, every 4h)
  └─ jobhunter.main
       ├─ ats.py       ~30 Canadian operators' own boards (Dayforce, UKG, ADP, Taleo, Phenom, iCIMS,
       │               SmartRecruiters, BambooHR, Rippling, Greenhouse, RSS...)
       ├─ sources.py   aggregators: Job Bank, LinkedIn (public search), Eluta.ca
       ├─ filters      role keywords + `country: canada` (config.yaml), so only relevant jobs cost API calls
       ├─ matcher.py   DeepSeek → score 0-100, verdict, met / missing / dealbreakers
       ├─ data/jobs.json   history of every posting (first seen, closed, evaluation), committed back
       └─ _site/       web/index.html + jobs.json → GitHub Pages
```

## Setup

1. **Profile.** Copy `profile.example.md` and fill it in. Be specific about licences, hours, medical,
   TSC / airport pass, driver's licence, and work authorization, because those are what postings gate on.
2. **Repository secrets** (Settings → Secrets and variables → Actions):
   - `DEEPSEEK_API_KEY`: from https://platform.deepseek.com
   - `CANDIDATE_PROFILE`: the full text of your filled-in profile
   - optional `USAJOBS_API_KEY` + `USAJOBS_EMAIL` for federal jobs (FAA, NTSB, DoD civilian)
3. **Pages:** Settings → Pages → Source: *GitHub Actions*.
4. **Filters:** `config.yaml` targets pilot / dispatcher / ramp titles located in Canada.
   Edit `include_title_keywords`, `exclude_title_keywords`, and the Job Bank `searches` to tune.
5. Run it from the Actions tab (*Poll jobs → Run workflow*), or wait for the schedule.

> **Privacy:** on a public repo, the Pages site and `data/jobs.json` are public too, and the
> match summaries quote facts from your profile. Keep the repo private (Pages on a private repo
> needs GitHub Pro), or accept that exposure.

## Run locally

```bash
pip install -r requirements.txt
cp profile.example.md profile.md   # then edit
export DEEPSEEK_API_KEY=sk-...
python -m jobhunter.main            # or --no-eval to poll only
python -m http.server -d _site      # open http://localhost:8000
pytest
```

## Behaviour

- **New postings** are evaluated newest first, up to `max_evaluations_per_run`; the rest stay
  *pending* until later runs.
- **Profile changes** trigger re-evaluation automatically, because each evaluation records a hash of the
  profile.
- **Duplicates** across sources (same title + city + overlapping company name) are folded into one
  card with "also on" links. Order sources in `config.yaml` by preference; the first one seen wins.
- **Closed postings** are marked *closed* (not deleted) once their board stops listing them. A board
  that errors during a run never closes its jobs.

## Sources

Operators polled directly (their board is authoritative, so aggregator copies of their jobs are dropped):
Air Canada (incl. Rouge, Cargo, ground handling), Jazz, Voyageur, WestJet / Encore, Porter, Air Transat,
PAL / Air Borealis / Provincial, Pacific Coastal, Harbour Air, KF Aerospace, Perimeter / Bearskin,
Calm Air, Keewatin Air, Canadian North, Air North, Cargojet, Morningstar, Rise Air, Air Tindi,
Central Mountain Air, Wasaya, Conair, Canadian Helicopters, Custom Helicopters, Carson Air,
Kenn Borek, and ground handlers Swissport, Menzies and GAT.

Aggregators cover the rest (Flair, Air Inuit, Ornge, Skyservice, Air Creebec, Propair, SkyCare...).
Flair, Summit, Skyservice and Air Creebec block automated access to their own careers pages.

## Adding companies

Look at the company's careers page URL:

| URL looks like | config entry |
|---|---|
| `boards.greenhouse.io/<slug>` or `job-boards.greenhouse.io/<slug>` | `{type: greenhouse, slug: <slug>, company: Name}` |
| `jobs.lever.co/<slug>` | `{type: lever, slug: <slug>, company: Name}` |
| `jobs.ashbyhq.com/<slug>` | `{type: ashby, slug: <slug>, company: Name}` |
| more Job Bank / LinkedIn / Eluta coverage | add search terms under that source's `searches` |

Other supported board types (copy an existing entry in `config.yaml` as a template):
`dayforce`, `ukg`, `adp`, `taleo`, `phenom`, `jibe`, `smartrecruiters`, `bamboohr`, `rippling`,
`squarespace`, `rss`. Add `aliases:` when an operator posts under another name on aggregators.
