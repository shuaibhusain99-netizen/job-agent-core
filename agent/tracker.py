"""Application tracker: tracker.csv in the repo, committed by the workflow so dedupe survives between runs."""
from __future__ import annotations
import csv
from datetime import date
from pathlib import Path

from .util import log

FIELDS = ["found_date", "score", "verdict", "title", "company", "location", "country", "source",
          "url", "status", "applied_date", "notes", "pack_file", "user_notes", "category", "sector", "salary", "apply_email"]


def load(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {r["url"]: r for r in csv.DictReader(f) if r.get("url")}


KEEP_DAYS = 90          # skipped / archived rows: kept this long so they aren't re-added as new
OPEN_DAYS = 45          # New / Review rows you never touched: postings close long before this
KEEP_STATUSES = {"Applied", "Interview", "Offer"}


def prune(rows: dict[str, dict]) -> int:
    """Keep the tracker small: drop untouched New/Review rows after OPEN_DAYS and archived rows after KEEP_DAYS.
    Anything you applied to, or wrote a note on, is never dropped."""
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    open_cut = (date.today() - timedelta(days=OPEN_DAYS)).isoformat()
    def stale(r):
        if r.get("status") in KEEP_STATUSES or (r.get("user_notes") or "").strip():
            return False
        d = r.get("found_date") or ""
        return d < (open_cut if r.get("status") in ("New", "Review") else cutoff)
    old = [u for u, r in rows.items() if stale(r)]
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
        "sector": j.get("sector", ""), "salary": j.get("salary", ""), "apply_email": j.get("apply_email", ""),
    }


REFRESH = ("category", "sector", "salary", "apply_email")


def refresh(row: dict, j: dict) -> bool:
    """A job already in the tracker was seen again: fill in fields an older version didn't record, and re-judge it if
    you haven't acted on it yet (status New/Review, no note) - so rules added later also clean up older matches."""
    changed = False
    if row.get("status") in ("New", "Review") and not (row.get("user_notes") or "").strip():
        if j["verdict"] == "reject":
            row["status"], row["notes"] = "Skipped", ("auto: " + j["reasons"])[:200]
            return True
        status = "New" if j["verdict"] == "apply" else "Review"
        if str(row.get("score")) != str(j["match_score"]) or row["status"] != status:
            row["score"], row["verdict"], row["status"], row["notes"] = j["match_score"], j["verdict"], status, j["reasons"][:200]
            changed = True
    for k in REFRESH:
        v = j.get(k, "")
        if v and not row.get(k):
            row[k] = v
            changed = True
    return changed


def sweep_titles(rows: dict[str, dict], profile: dict) -> int:
    """Rules that only need the title (nationals-only, clearly other disciplines) applied to every row you haven't acted on."""
    import re
    rejects = profile.get("title_reject_patterns", [])
    heavy = [(t, p) for t, p in profile.get("title_penalties", {}).items() if not t.startswith("_") and isinstance(p, (int, float)) and p <= -50]
    n = 0
    for r in rows.values():
        if r.get("status") not in ("New", "Review") or (r.get("user_notes") or "").strip():
            continue
        title = (r.get("title") or "").lower()
        why = next((f"title rejects: /{p}/" for p in rejects if re.search(p, title, re.I)), None) or \
              next((f"title:{t}({p})" for t, p in heavy if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", title)), None)
        if why:
            r["status"], r["notes"] = "Skipped", ("auto: " + why)[:200]
            n += 1
    return n


def backfill_from_notes(rows: dict[str, dict]) -> int:
    """Older rows: recover sector / pay from the scoring notes, which recorded them."""
    import re
    n = 0
    for r in rows.values():
        notes = r.get("notes", "")
        if not r.get("sector"):
            m = re.search(r"sector:(\w+)", notes)
            if m:
                r["sector"] = m.group(1); n += 1
        if not r.get("salary"):
            m = re.search(r"pay: ([^;]+)", notes)
            if m:
                r["salary"] = m.group(1).strip(); n += 1
    return n

