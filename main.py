#!/usr/bin/env python3
"""Civil Engineer Job Agent — search, match, prepare, track.

Run:  python main.py            (full daily run)
      python main.py --dry      (no sheet push, no tracker write; prints results)
Needs only the Python standard library.
"""
from __future__ import annotations
import argparse, json, os, sys
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
    seen_tc: set[str] = set()
    for j in raw:
        k = dedupe_key(j)
        tc = (j["title"].lower().strip() + "|" + j["company"].lower().strip()) if j["company"] != "Unknown" else ""
        if k and k not in unique and (not tc or tc not in seen_tc):
            unique[k] = j
            if tc:
                seen_tc.add(tc)
    log(f"Collected {len(raw)} rows, {len(unique)} unique")

    # 2. first-pass score on title + snippet, then fetch full text for the promising ones and rescore
    jobs = list(unique.values())
    for j in jobs:
        j["match_score"], j["verdict"], r = score(j, PROFILE)
        j["reasons"] = "; ".join(r)
    jobs.sort(key=lambda j: -j["match_score"])
    # Full text is fetched only for jobs not already in the tracker (they were judged on an earlier day),
    # best first, so the budget goes where it changes a decision.
    known = tracker.load(TRACKER)
    tracker.backfill_from_notes(known)
    swept = tracker.sweep_titles(known, PROFILE)
    if swept:
        log(f"Archived {swept} listed jobs that today's title rules exclude (nationals-only, other disciplines)")
    budget = args.max_enrich
    for j in jobs:
        if budget <= 0:
            break
        if j["verdict"] == "reject" or j["url"] in known:
            continue
        before = len(j.get("description", ""))
        enrich(j)
        if len(j.get("description", "")) != before:
            budget -= 1
            j["match_score"], j["verdict"], r = score(j, PROFILE)
            j["reasons"] = "; ".join(r)
    jobs.sort(key=lambda j: -j["match_score"])
    log(f"Fetched full descriptions for {args.max_enrich - budget} new candidates")

    # 3. dedupe against tracker, prepare packs for new matches
    new_rows = []
    refreshed = 0
    for j in jobs:
        if j["url"] in known:
            refreshed += tracker.refresh(known[j["url"]], j)
            continue
        if j["verdict"] == "reject":
            continue
        if j["verdict"] == "apply" and j.get("country") not in countries(PROFILE, "apply"):
            j["verdict"] = "found"          # country searched but applying switched off
        if j["verdict"] == "apply":
            safe = "".join(c if c.isalnum() else "-" for c in f"{j['company']}-{j['title']}")[:70].strip("-")
            pack = PACKS / f"{date.today():%Y-%m-%d}-{safe}.md"
            pack.write_text(application_pack(j, PROFILE), encoding="utf-8")
            j["pack_file"] = str(pack.relative_to(ROOT))
        row = tracker.row_from_job(j)
        new_rows.append(row)
        known[j["url"]] = row

    # 4. report
    stamp = date.today().isoformat()
    report = REPORTS / f"jobs_{stamp}.csv"
    import csv
    with report.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["match_score", "verdict", "title", "company", "location", "country", "source", "url", "reasons"], extrasaction="ignore")
        w.writeheader()
        w.writerows(j for j in jobs if j["verdict"] != "reject")

    apply_new = [r for r in new_rows if r["verdict"] == "apply"]
    log(f"Re-checked {refreshed} jobs already in the list (updated details or scores; rejects moved to Archived)")
    print(f"\n=== {stamp}: {len(jobs)} jobs scanned, {sum(1 for j in jobs if j['verdict']!='reject')} relevant, "
          f"{len(new_rows)} new, {len(apply_new)} ready to apply ===")
    for r in sorted(new_rows, key=lambda r: -int(r["score"]))[:25]:
        print(f"[{int(r['score']):>3}] {r['verdict']:<6} {r['title']} — {r['company']} ({r['location']}) [{r['source']}]")
        print(f"       {r['url']}")
        if r.get("pack_file"):
            print(f"       pack: {r['pack_file']}")

    if args.dry:
        print("\n(dry run: tracker and sheet not updated)")
        return 0
    tracker.save(TRACKER, known)
    # expose counts to the workflow for the run summary
    gh = os.getenv("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"new={len(new_rows)}\napply={len(apply_new)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
