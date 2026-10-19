# Civil Engineer Job Agent

A free, daily job agent for Shuaib's GCC civil-engineering search. Every morning it:

1. **Searches** Saudi Arabia, UAE, Qatar, Kuwait, Oman, Bahrain and Canada for Civil / Site / Project / QA-QC / Infrastructure / Water engineer roles across job boards and employer career sites.
2. **Matches** each posting to `profile.json` — your real experience, skills and certificates — and scores it 0-100 with the reasons shown. Roles asking 7+ years, nationals-only, other disciplines or management level are rejected automatically.
3. **Prepares** an application pack for every match: a tailored CV summary, a cover letter built only from factual CV blocks, and draft answers to the posting's screening questions.
4. **Tracks** everything in `tracker.csv` (committed to the repo) and mirrors new rows to your Google Sheet, where you update the status as you apply.

Cost: zero. Standard-library Python only, runs on GitHub Actions' free tier.

**What it deliberately does not do:** press Submit. Most portals require an account and a human on the final step, and auto-submitting also tends to get accounts flagged. The agent takes you to the point where applying is a link, a paste and a click.

## Sources

| Source | Needs | Notes |
|---|---|---|
| Jooble API | free key (`JOOBLE_API_KEY`) | Aggregates Bayt, GulfTalent, Naukrigulf, Indeed and company sites across all 6 countries. **Set this up — it is the main feed.** |
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

### 3. Google Sheet tracker (3 minutes)
1. Create a Google Sheet called e.g. *Job Applications*. Rename the first tab to `Applications`.
2. Extensions → Apps Script. Delete the sample code, paste in `apps_script.gs`, change `TOKEN` to any long random string, save.
3. Deploy → New deployment → type **Web app** → Execute as **Me** → Who has access **Anyone** → Deploy → authorise → copy the **Web app URL**.
4. Add two more repo secrets: `SHEET_WEBHOOK_URL` = that URL, `SHEET_WEBHOOK_TOKEN` = your token.

Without step 3 the agent still works; `tracker.csv` in the repo is the record.

### 4. First run
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

## Control panel (claude.ai)
A private page "Job Agent Control" holds the settings (country search/apply switches, minimum score, pause, run now) and the tracker with packs. A daily scheduled task in Claude (07:30 Riyadh) runs `sync/sync_tool.py`: it applies the panel's settings to `profile.json` (and dispatches a run if "Run now" was pressed), merges status edits back into `tracker.csv`, and loads new jobs and packs into the panel.
