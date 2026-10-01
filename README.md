# Job Hunter ✈️

Polls aviation and aerospace job boards every few hours. DeepSeek screens each new posting
against your profile, and every posting goes on a filterable webpage hosted on GitHub Pages.

```
GitHub Actions (cron, every 4h)
  └─ jobhunter.main
       ├─ sources.py   fetch Greenhouse / Lever / Ashby / USAJOBS boards from config.yaml
       ├─ filters      title/location keywords (config.yaml), so only relevant jobs cost API calls
       ├─ matcher.py   DeepSeek → score 0-100, verdict, met / missing / dealbreakers
       ├─ data/jobs.json   history of every posting (first seen, closed, evaluation), committed back
       └─ _site/       web/index.html + jobs.json → GitHub Pages
```

## Setup

1. **Profile.** Copy `profile.example.md` and fill it in. Be specific about certificates, hours,
   medical, clearance, and citizenship, because those are what aviation postings gate on.
2. **Repository secrets** (Settings → Secrets and variables → Actions):
   - `DEEPSEEK_API_KEY`: from https://platform.deepseek.com
   - `CANDIDATE_PROFILE`: the full text of your filled-in profile
   - optional `USAJOBS_API_KEY` + `USAJOBS_EMAIL` for federal jobs (FAA, NTSB, DoD civilian)
3. **Pages:** Settings → Pages → Source: *GitHub Actions*.
4. **Filters:** set `include_title_keywords` in `config.yaml` to your target roles. Without it,
   every posting is evaluated (thousands at SpaceX alone), which burns through the
   per-run cap slowly.
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
- **Closed postings** are marked *closed* (not deleted) once their board stops listing them. A board
  that errors during a run never closes its jobs.

## Adding companies

Look at the company's careers page URL:

| URL looks like | config entry |
|---|---|
| `boards.greenhouse.io/<slug>` or `job-boards.greenhouse.io/<slug>` | `{type: greenhouse, slug: <slug>, company: Name}` |
| `jobs.lever.co/<slug>` | `{type: lever, slug: <slug>, company: Name}` |
| `jobs.ashbyhq.com/<slug>` | `{type: ashby, slug: <slug>, company: Name}` |

Airlines and large OEMs (Boeing, Delta, United…) mostly use Workday or custom portals, which
aren't supported yet.
