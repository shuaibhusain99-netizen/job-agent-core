"""Application tracker.

Primary store: tracker.csv in the repo (committed by the workflow, so dedupe survives between runs).
Mirror: a Google Sheet, written through a tiny Apps Script web app (see apps_script.gs) when
the SHEET_WEBHOOK_URL and SHEET_WEBHOOK_TOKEN secrets are set.
"""
from __future__ import annotations
import csv, json, os
from datetime import date
from pathlib import Path

from .util import http, log

FIELDS = ["found_date", "score", "verdict", "title", "company", "location", "country", "source",
          "url", "status", "applied_date", "notes", "pack_file"]


def load(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r["url"]: r for r in csv.DictReader(f) if r.get("url")}


def save(path: Path, rows: dict[str, dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (r.get("found_date", ""), -int(r.get("score") or 0)), reverse=True):
            w.writerow(r)


def row_from_job(j: dict) -> dict:
    return {
        "found_date": date.today().isoformat(), "score": j["match_score"], "verdict": j["verdict"],
        "title": j["title"], "company": j["company"], "location": j["location"], "country": j.get("country", ""),
        "source": j["source"], "url": j["url"],
        "status": {"apply": "New", "found": "Found (apply off)"}.get(j["verdict"], "Review"), "applied_date": "", "notes": j["reasons"][:200],
        "pack_file": j.get("pack_file", ""),
    }


def push_to_sheet(new_rows: list[dict]) -> None:
    url = os.getenv("SHEET_WEBHOOK_URL", "").strip()
    token = os.getenv("SHEET_WEBHOOK_TOKEN", "").strip()
    if not url or not token:
        log("Google Sheet: skipped (SHEET_WEBHOOK_URL / SHEET_WEBHOOK_TOKEN not set)")
        return
    if not new_rows:
        log("Google Sheet: nothing new to add")
        return
    payload = json.dumps({"token": token, "rows": [[r.get(f, "") for f in FIELDS] for r in new_rows]}).encode()
    status, body = http(url, data=payload, headers={"Content-Type": "application/json"}, timeout=40)
    log(f"Google Sheet: HTTP {status} {body[:120]}")
