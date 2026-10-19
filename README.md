# Civil Engineer Job Agent

A free, daily job agent for Shuaib's civil-engineering search across the GCC, Canada and Australia. Every morning it:

1. **Searches** Saudi Arabia, UAE, Qatar, Kuwait, Oman, Bahrain and Canada for Civil / Site / Project / QA-QC / Infrastructure / Water engineer roles across job boards and employer career sites.
2. **Matches** each posting to `profile.json` — your real experience, skills and certificates — and scores it 0-100 with the reasons shown. Roles asking 7+ years, nationals-only, other disciplines or management level are rejected automatically.
3. **Prepares** an application pack for every match: a tailored CV summary, a cover letter built only from factual CV blocks, and draft answers to the posting's screening questions.
4. **Tracks** everything in `tracker.csv` (committed to the repo), which the control page shows and edits.

Cost: zero. Standard-library Python only, runs on GitHub Actions' free tier.

**What it deliberately does not do:** press Submit. Most portals require an account and a human on the final step, and auto-submitting also tends to get accounts flagged. The agent takes you to the point where applying is a link, a paste and a click.

## Sources

| Source | Needs | Notes |
|---|---|---|
| Jooble API | free key (`JOOBLE_API_KEY`) | Canada aggregator (this key's coverage is weak for the Gulf). |
| SEEK | nothing | Australia's main board, public API. |
| LinkedIn public listings | nothing | Covers every country incl. Canada. Works most days; LinkedIn sometimes rate-limits cloud IPs, in which case it's skipped for that run. |
| Workday career sites (Parsons, KBR, …) | nothing | Reliable. Add more employers in `sources.json`. |
| SuccessFactors (alfanar, …) | nothing | Reliable. Add more in `sources.json`. |
| Workable recruiters | nothing | Rate-limited; may skip some runs. |
| Google Alerts RSS | free feed URLs | Optional catch-all for anything Google indexes. |
| GulfTalent HTML | nothing | Blocked from most cloud IPs; included in case it works from yours. |

## Setup (about 15 minutes, once)

### 1. GitHub repository
1. Create a **private** repo at https://github.com/new (e.g. `job-agent`).
2. Upload everything in this folder, keeping `.github/workflows/daily.yml` and the `agent/` folder.
3. Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save (the run commits the tracker back).

### 2. Jooble API key (free, 2 minutes)
1. Go to https://jooble.org/api/about and request a key (instant, email confirmation).
2. Repo → Settings → Secrets and variables → Actions → **New repository secret**: `JOOBLE_API_KEY` = the key.

### 3. First run
Actions tab → **Daily job agent** → Run workflow. Open the run → the summary shows the day's list; the `job-report` artifact has the CSV and the application packs; `tracker.csv` and `applications/` are committed to the repo. From then on it runs daily at 07:00 Riyadh time.

### Optional: Google Alerts
At https://www.google.com/alerts create alerts such as `"civil engineer" jobs Saudi Arabia`, set *Deliver to: RSS feed*, copy each feed URL into `sources.json → google_alerts_rss.feeds`.

## Daily routine (yours, 10 minutes)
1. Open the Google Sheet (or the run summary). Rows with status **New** are ready to apply.
2. Open the row's `pack_file` in the repo (`applications/…md`): copy the summary / cover letter / answers into the portal, attach your CV PDF, submit.
3. Set the row's status to **Applied** and fill `applied_date`. Use **Skipped** for ones you pass on so they don't nag.

## Tuning
- `profile.json → search.min_score` (default 55): lower it to see more "maybe" roles, raise it to be stricter.
- `profile.json → search.countries`: one entry per country, e.g. `"Bahrain": {"weight": 0.7, "search": true, "apply": false}`.
  `search:false` stops looking there; `apply:false` keeps logging matches (status *Found (apply off)*) but makes no application pack;
  `weight` (0-1) nudges the score. Add a country by adding an entry (and its city names to `COUNTRY_HINTS` in `agent/util.py` if not already there).
- `title_penalties`, `keyword_points`, `hard_reject_patterns`: the whole scoring logic is these three tables — no code changes needed.
- `evidence_blocks` and `screening_answers`: the only text the cover letters and answers are built from. Keep them factual; add a block when you gain new experience.
- `sources.json → workday.boards`: to add an employer, open their careers site, note the URL `https://<tenant>.<host>.myworkdayjobs.com/<site>` and add `{tenant, host, site}`.

## Run locally
```
python main.py --dry     # search + score + packs, nothing written to tracker/sheet
python main.py           # full run
```
Python 3.10+, no packages to install.

## Files
```
main.py              entry point
agent/sources.py     fetchers (one function per source) + full-text enrichment
agent/score.py       scoring rules
agent/tailor.py      summary / cover letter / screening answers from evidence blocks
agent/tracker.py     tracker.csv + Google Sheet push
profile.json         your matching profile and CV evidence (edit this)
sources.json         which sources/employers to search (edit this)
apps_script.gs       paste into the Google Sheet's Apps Script
tracker.csv          running record of every relevant job found
applications/        one markdown pack per match
reports/             daily CSV of everything relevant that run
```

## Control page
https://shuaibhusain99-netizen.github.io/civil-engineer-job-agent/ — settings (countries, score, pause, run now) and the tracker (To apply / Review / Applied / Archived) with application packs. It talks to this repository from your browser with a fine-grained GitHub token (Contents + Actions read/write) that stays on your device.
