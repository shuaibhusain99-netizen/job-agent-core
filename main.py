#!/usr/bin/env python3
"""Job Agent — search, match, prepare, track. Run by the desktop app (app.py) with ROOT etc. pointed at its data folder.

Run:  python main.py            (full daily run)
      python main.py --dry      (no sheet push, no tracker write; prints results)
Needs only the Python standard library.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import date
from pathlib import Path

from agent.sources import fetch_all, enrich
from agent.score import score
from agent.tailor import application_pack
from agent import tracker
from agent.util import dedupe_key, log, countries

ROOT = Path(__file__).resolve().parent
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
TRACKER = ROOT / "tracker.csv"
REPORTS = ROOT / "reports"
PACKS = ROOT / "applications"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="don't write tracker/sheet")
    ap.add_argument("--max-enrich", type=int, default=350, help="how many new candidates get full descriptions fetched")
    args = ap.parse_args()
    REPORTS.mkdir(exist_ok=True)
    PACKS.mkdir(exist_ok=True)
    if PROFILE["search"].get("paused"):
        print("Agent is paused (profile.json search.paused = true). Nothing to do.")
        return 0

    # 1. search
    raw = fetch_all(SOURCES, PROFILE)
    unique: dict[str, dict] = {}
    seen_tcc: set[str] = set()
    for j in raw:
        k = dedupe_key(j)
        # the same title at the same company in the same country is one job, wherever it was found
        tcc = "|".join([j["title"].lower().strip(), j["company"].lower().strip(), j.get("country") or j["location"].lower()]) \
            if j["company"] != "Unknown" else ""
        if k and k not in unique and (not tcc or tcc not in seen_tcc):
            unique[k] = j
            if tcc:
                seen_tcc.add(tcc)
    log(f"Collected {len(raw)} rows, {len(unique)} unique")

    # 2. the job list as it stands (by the same keys, so a LinkedIn job seen on another subdomain is still "known")
    known = tracker.load(TRACKER)
    tracker.backfill_from_notes(known)
    known_key = {dedupe_key(r): u for u, r in known.items()}
    for r in tracker.sweep_titles(known, PROFILE):
        drop_pack(r)
    swept = sum(1 for r in known.values() if (r.get("notes") or "").startswith("auto: title"))
    if swept:
        log(f"Archived listed jobs that today's title rules exclude (nationals-only, other disciplines): {swept} in total")

    # 3. score on what the listing gives, then fetch the full ad for promising jobs and re-score.
    #    Known jobs are re-read only if they are still open in your list and were judged on a snippet.
    jobs = list(unique.values())
    for j in jobs:
        j["match_score"], j["verdict"], r = score(j, PROFILE)
        j["reasons"] = "; ".join(r)
        j["_known"] = known_key.get(dedupe_key(j))
    jobs.sort(key=lambda j: -j["match_score"])
    budget = args.max_enrich
    for j in jobs:
        if budget <= 0:
            break
        if j["verdict"] == "reject":
            continue
        if j["_known"]:
            row = known[j["_known"]]
            if not tracker._untouched(row) or tracker._known_len(row) >= 400:
                continue
        before = len(j.get("description", ""))
        enrich(j)
        if len(j.get("description", "")) != before:
            budget -= 1
            j["match_score"], j["verdict"], r = score(j, PROFILE)
            j["reasons"] = "; ".join(r)
    jobs.sort(key=lambda j: -j["match_score"])
    log(f"Fetched full descriptions for {args.max_enrich - budget} jobs")

    # 4. update known jobs, add new ones, write application packs for new matches
    apply_countries = countries(PROFILE, "apply")
    new_rows, updated, skipped = [], 0, 0
    for j in jobs:
        if j["_known"]:
            row = known[j["_known"]]
            res = tracker.refresh(row, j, apply_countries)
            updated += res == "updated"
            if res == "skipped":
                skipped += 1
                drop_pack(row)
            continue
        if j["verdict"] == "reject":
            continue
        if j["verdict"] == "apply" and j.get("country") not in apply_countries:
            j["verdict"] = "found"          # country searched but applying switched off
        if j["verdict"] == "apply":
            safe = "".join(c if c.isalnum() else "-" for c in f"{j['company']}-{j['title']}")[:60].strip("-")
            tag = hashlib.sha1(j["url"].encode()).hexdigest()[:6]
            pack = PACKS / f"{date.today():%Y-%m-%d}-{safe}-{tag}.md"
            pack.write_text(application_pack(j, PROFILE), encoding="utf-8")
            j["pack_file"] = pack.relative_to(ROOT).as_posix()
        row = tracker.row_from_job(j)
        new_rows.append(row)
        known[j["url"]] = row

    # 5. report
    stamp = date.today().isoformat()
    report = REPORTS / f"jobs_{stamp}.csv"
    import csv
    with report.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["match_score", "verdict", "title", "company", "location", "country", "source", "url", "reasons"], extrasaction="ignore")
        w.writeheader()
        for j in jobs:
            if j["verdict"] != "reject":
                w.writerow({k: tracker._esc(j.get(k, "")) for k in w.fieldnames})

    apply_new = [r for r in new_rows if r["verdict"] == "apply"]
    log(f"Jobs already in your list: {updated} updated, {skipped} moved to Archived by today's rules")
    print(f"\n=== {stamp}: {len(jobs)} jobs scanned, {sum(1 for j in jobs if j['verdict']!='reject')} relevant, "
          f"{len(new_rows)} new, {len(apply_new)} ready to apply ===")
    for r in sorted(new_rows, key=lambda r: -int(r["score"]))[:25]:
        print(f"[{int(r['score']):>3}] {r['verdict']:<6} {r['title']} — {r['company']} ({r['location']}) [{r['source']}]")
        print(f"       {r['url']}")
        if r.get("pack_file"):
            print(f"       pack: {r['pack_file']}")

    if args.dry:
        print("\n(dry run: job list not updated)")
        return 0
    tracker.save(TRACKER, known)
    return 0


def drop_pack(row: dict) -> None:
    """Delete the application pack of a job the rules have archived (only files inside the packs folder)."""
    rel = row.get("pack_file") or ""
    if not rel:
        return
    p = (ROOT / rel).resolve()
    if p.is_relative_to(PACKS.resolve()) and p.suffix == ".md":
        p.unlink(missing_ok=True)
    row["pack_file"] = ""


if __name__ == "__main__":
    sys.exit(main())
