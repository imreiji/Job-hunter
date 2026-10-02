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
   - `SITE_PASSWORD` (**required**): the password that unlocks the site. Use a long passphrase,
     e.g. five random words. The encrypted data is publicly downloadable, so a short password could
     be guessed offline. The poller refuses to run with fewer than 12 characters.
   - `DEEPSEEK_API_KEY`: from https://platform.deepseek.com
   - `CANDIDATE_PROFILE`: the full text of your filled-in profile
   - optional `USAJOBS_API_KEY` + `USAJOBS_EMAIL` for federal jobs (FAA, NTSB, DoD civilian)
3. **Pages:** Settings → Pages → Source: *GitHub Actions*.
4. **Filters:** `config.yaml` targets pilot / dispatcher / ramp titles located in Canada.
   Edit `include_title_keywords`, `exclude_title_keywords`, and the Job Bank `searches` to tune.
5. Run it from the Actions tab (*Poll jobs → Run workflow*), or wait for the schedule.

> **Privacy:** keep this repository private: `data/jobs.json` in it is unencrypted. GitHub Pages
> from a private repository needs a paid plan, and even then the site URL itself is public.
> That's why the site only ever serves encrypted data (see Security below).

## The webpage

- **Jobs:** every posting with its DeepSeek score, filterable by role (pilot / dispatch / ground),
  company and verdict. **Apply ↗** opens the application form directly where the board exposes one
  (Workday, Dayforce, Taleo, UKG, iCIMS, SmartRecruiters, Rippling, Lever, Ashby, FedEx, UPS…),
  otherwise the posting. Use **Track…** to save a job to the tracker.
- **Applications:** status (saved → applied → interview → offer / rejected / withdrawn), applied and
  follow-up dates, notes, and a status history. Clicking Apply saves the job, and when you return to
  the tab it asks whether you applied. Jobs found elsewhere can be added manually.
  - Data lives in your browser (`localStorage`). **Export / Import** moves it between browsers.
  - **Sync…** (optional) stores it as a JSON file in a GitHub repo of your choice, using a
    fine-grained token with *Contents: read and write* on that one repo. Prefer a private repo.
    If you sync to *this* repo at `data/applications.json`, the poller also keeps tracked jobs on
    the board even if they later fall outside your filters.
- **Trends:** open postings over time by role, new postings per week, most active employers
  (30 days), and how long postings stay open. Each chart has a table view. Everything is computed
  from `data/jobs.json`, so history starts when tracking began.

## Security

GitHub Pages can't require a login, so access control is done with encryption instead of a server:

- **Encrypted site.** The poller encrypts the job data with AES-256-GCM, using a key derived from
  `SITE_PASSWORD` with PBKDF2-SHA256 (600,000 iterations), and publishes only `jobs.enc.json`. The page
  asks for the password and decrypts in your browser. A wrong password or a tampered file fails to
  decrypt. CI refuses to run without the secret and fails if a plaintext `jobs.json` ever lands in the
  site build. The page markup and script are public, but contain no data.
- **Remember on this device** keeps the derived key in IndexedDB as a non-extractable `CryptoKey`.
  **Lock** forgets it. Changing `SITE_PASSWORD` locks every device on the next poll.
- **Browser storage is encrypted.** Tracker data and the sync token are stored encrypted with the same
  key. The synced `applications.json` is encrypted too, so it is safe even in a public repo.
- **Untrusted data is escaped and validated.** Job text is scraped from the web, so everything is
  HTML-escaped. Only `http(s)` links are allowed (in the poller and again in the page). Statuses, roles
  and verdicts are checked against allowlists, including in imported or synced files.
- **Content Security Policy:** only the site's own script runs, and the only outside host the page can
  call is `api.github.com`. Links open with `noopener noreferrer`, and the page is marked `noindex`.
- **DeepSeek** sees postings fenced as untrusted data and is told to ignore instructions inside them.
  Its output is validated and size-limited before it's stored.
- **CI least privilege:** the job that handles scraped data and API keys can only push commits. Pages
  deployment runs in a separate job with the Pages permissions.

### Changing the site password

1. Update the `SITE_PASSWORD` secret, then run **Poll jobs** (or wait for the next scheduled run). Job data
   lives unencrypted in this private repo and is simply re-encrypted with the new password.
2. Every device is locked out, including ones set to "Remember on this device". Sign in with the new password.
3. Your tracker data (and sync settings) were encrypted with the old password. They are kept, never overwritten,
   and the page shows **Recover tracker data**. Enter the previous password once on each device. If you use
   sync, the synced file is re-encrypted with the new password at the same time.
4. Lost the old password? Choose **Discard old data**. On a synced device this overwrites the old file
   with what the browser currently has.

Residual risks worth knowing:
- **Same-origin pages.** Pages on `<you>.github.io` share one browser origin with your other project
  sites. Stored data stays encrypted, but a script on another of your project sites could *use* a
  remembered key. Use a custom domain, or skip "Remember", if you host untrusted code there.
- **Unpinned actions.** GitHub Actions are pinned by version tag, not commit SHA.
- **Token scope.** Give the sync token an expiry and access to one repository only.

## Run locally

```bash
pip install -r requirements.txt
cp profile.example.md profile.md   # then edit
export DEEPSEEK_API_KEY=sk-...
export SITE_PASSWORD='your site passphrase'
python -m jobhunter.main            # or --no-eval to poll only
python -m http.server -d _site      # open http://localhost:8000
# for UI work without a password: python -m jobhunter.main --no-eval --allow-plaintext-site
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
Kenn Borek.

Ground side: Swissport, Menzies, GAT, Alliance Ground (AGI), FedEx, UPS and DHL (cargo ramp / gateways),
Integrated Deicing Services / Inland and Aeromag (de-icing, into-plane fuel), Chartright and Execaire
(FBO line service), and airside jobs at the Toronto Pearson (GTAA), Vancouver, Calgary, Edmonton and
Winnipeg airport authorities.

Aggregators cover the rest (Air Inuit, Ornge, Skyservice, Million Air, EFC, Air Creebec, Propair, SkyCare...).
Summit, Skyservice, Air Creebec, Aéroports de Montréal and Ledcor block automated access to their careers pages.

## Adding companies

Look at the company's careers page URL:

| URL looks like | config entry |
|---|---|
| `boards.greenhouse.io/<slug>` or `job-boards.greenhouse.io/<slug>` | `{type: greenhouse, slug: <slug>, company: Name}` |
| `jobs.lever.co/<slug>` | `{type: lever, slug: <slug>, company: Name}` |
| `jobs.ashbyhq.com/<slug>` | `{type: ashby, slug: <slug>, company: Name}` |
| more Job Bank / LinkedIn / Eluta coverage | add search terms under that source's `searches` |

Other supported board types (copy an existing entry in `config.yaml` as a template):
`dayforce`, `ukg`, `adp`, `workday`, `taleo`, `phenom`, `jibe`, `paradox`, `successfactors`, `radancy`,
`smartrecruiters`, `bamboohr`, `rippling`, `squarespace`, `rss`. Add `aliases:` when an operator posts under another name on aggregators.
