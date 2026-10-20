# Job Agent (build repository)

This repository only builds **JobAgent.exe** for Windows. It must stay **private**.

The app runs entirely on the user's PC: searching, scoring, the job list, application packs, e-mail settings, the CV and the
apply-browser profile all live in a `data` folder next to the exe and are never uploaded anywhere.
Personal data (tracker, packs, CV, mail settings, sent mail) is ignored by git — see `.gitignore`.

* `app.py` — local app: server on 127.0.0.1 with a per-launch token, control page, e-mail sending, apply window
* `main.py`, `agent/` — search → score → packs → job list
* `agent/browser.py`, `agent/filler.js` — "Apply on site": fills forms in an isolated browser world, never submits
* `profile.json` — candidate facts (yours) + rule sets (program-maintained, `_rules_version`)
* `tests/` — run on Windows before every build: `python -m unittest discover -s tests -t .` (no real website is contacted)
* `.github/workflows/build-app.yml` — builds and smoke-tests the exe on a Windows runner, publishes it as a private release
