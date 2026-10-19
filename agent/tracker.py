"""Application tracker: tracker.csv in the repo, committed by the workflow so dedupe survives between runs."""
from __future__ import annotations
import csv
from datetime import date
from pathlib import Path

from .util import log

FIELDS = ["found_date", "score", "verdict", "title", "company", "location", "country", "source",
          "url", "status", "applied_date", "notes", "pack_file", "user_notes", "category"]


def load(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r["url"]: r for r in csv.DictReader(f) if r.get("url")}


KEEP_DAYS = 90
KEEP_STATUSES = {"Applied", "Interview", "Offer", "New", "Review"}


def prune(rows: dict[str, dict]) -> int:
    """Drop rejected/skipped/apply-off rows older than KEEP_DAYS so the tracker stays small."""
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    old = [u for u, r in rows.items() if r.get("status") not in KEEP_STATUSES and (r.get("found_date") or "") < cutoff]
    for u in old:
        rows.pop(u, None)
    return len(old)


def save(path: Path, rows: dict[str, dict]) -> None:
    prune(rows)
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
        "pack_file": j.get("pack_file", ""), "user_notes": "", "category": j.get("category", "civil"),
    }

