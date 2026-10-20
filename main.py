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

from agent.sources import fetch_all, enrich_many, load_keys
from agent import sitebrowse
from agent.score import score
from agent.tailor import application_pack
from agent import tracker, ai, details
from agent.util import dedupe_key, log, countries
from agent import util

ROOT = Path(__file__).resolve().parent
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
TRACKER = ROOT / "tracker.csv"
REPORTS = ROOT / "reports"
PACKS = ROOT / "applications"


CATEGORY_NAMES = {"civil"} | {k for k in PROFILE.get("categories", {}) if not k.startswith("_")}


def main() -> int:
    global CATEGORY_NAMES
    CATEGORY_NAMES = {"civil"} | {k for k in PROFILE.get("categories", {}) if not k.startswith("_")}
    details.ROOT = ROOT / "details"
    sitebrowse.PROFILE_DIR = ROOT / "browser-search"
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="don't write tracker/sheet")
    ap.add_argument("--max-enrich", type=int, default=300, help="how many jobs get their full ad fetched (best first)")
    ap.add_argument("--full", action="store_true", help="search the full period, not only what's new since the last search")
    args = ap.parse_args()
    REPORTS.mkdir(exist_ok=True)
    PACKS.mkdir(exist_ok=True)
    if PROFILE["search"].get("paused"):
        print("Agent is paused (profile.json search.paused = true). Nothing to do.")
        return 0

    # 1. search - only what's new since the last search, with a full search every few days
    known = tracker.load(TRACKER)
    util.KNOWN = {dedupe_key(r) for r in known.values()}
    full, sig = plan_search(args.full)
    log("Full search (all recent ads)" if not util.WINDOW_DAYS else
        f"Quick search: ads from the last {util.WINDOW_DAYS} days (a full search runs every {full_every()} days)")
    raw = fetch_all(SOURCES, PROFILE)
    unique: dict[str, dict] = {}
    by_tcc: dict[str, str] = {}
    for j in raw:
        k = dedupe_key(j)
        if not k:
            continue
        # the same title at the same company in the same country is one job, wherever it was found:
        # keep the first one seen, but take the fuller ad text from the copy that has it (no need to fetch it again)
        tcc = "|".join([j["title"].lower().strip(), j["company"].lower().strip(), j.get("country") or j["location"].lower()]) \
            if j["company"] != "Unknown" else ""
        first = unique.get(k) or (unique.get(by_tcc[tcc]) if tcc in by_tcc else None)
        if first is None:
            unique[k] = j
            if tcc:
                by_tcc[tcc] = k
        else:
            if len(j.get("description", "")) > len(first.get("description", "")) and not j.get("_snippet"):
                first["description"] = j["description"]
                first.pop("_snippet", None)
            if not first.get("posted") and j.get("posted"):
                first["posted"] = j["posted"]
    log(f"Collected {len(raw)} rows, {len(unique)} unique")

    # 2. the job list as it stands (by the same keys, so a LinkedIn job seen on another subdomain is still "known")
    tracker.backfill_from_notes(known)
    n = tracker.retag_old_rows(known, PROFILE)
    if n:
        log(f"Re-tagged {n} rows saved by an older version")
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
    want = []
    for j in jobs:
        if j["verdict"] == "reject":
            continue
        if j["_known"]:
            row = known[j["_known"]]
            if not tracker._untouched(row) or tracker._known_len(row) >= 400:
                continue
        want.append(j)
    try:
        fetched = enrich_many(want, args.max_enrich)
    finally:
        sitebrowse.close()                      # the search browser is not needed after this
    for j in want:
        if len(j.get("description", "")) >= 300:
            j["match_score"], j["verdict"], r = score(j, PROFILE)
            j["reasons"] = "; ".join(r)
    jobs.sort(key=lambda j: -j["match_score"])
    log(f"Fetched full descriptions for {fetched} jobs")

    # 3b. AI reviewer (optional): reads the promising ads like a recruiter and has the final say on fit
    keys = load_keys()
    if ai.settings(keys):
        ai.USAGE = ROOT / "ai_usage.json"
        cand = [j for j in jobs if j["verdict"] != "reject" and (not j["_known"] or tracker._untouched(known[j["_known"]]))]
        ai.review_many(cand, PROFILE, keys, cache_get=lambda u: details.load(u).get("ai") or {},
                       cache_put=lambda u, r: details.save(u, {"ai": r}))
        for j in cand:
            r = j.get("ai")
            if not r:
                continue
            cat = r["role_family"] if r["role_family"] in CATEGORY_NAMES else j.get("category")
            sp = PROFILE["search"]
            thr = sp["min_score"] if cat == "civil" else (sp.get("min_score_oilgas", sp["min_score"]) if cat == "oil_gas" else sp.get("min_score_other", sp["min_score"] + 5))
            j["match_score"], j["verdict"] = ai.combine(j["match_score"], j["verdict"], r, thr)
            j["category"] = cat
            j["level"] = r["level"] or j.get("level", "")
            j["reasons"] = (f"AI {r['fit']}: {r['summary']}" + (f" | deal-breakers: {'; '.join(r['deal_breakers'])}" if r['deal_breakers'] else "")
                            + " || rules: " + j["reasons"])
        jobs.sort(key=lambda j: -j["match_score"])

    # keep the ad text for the app's detail view
    for j in jobs:
        if j["verdict"] != "reject" and len(j.get("description", "")) >= 80:
            details.save(j["url"], {"title": j["title"], "company": j["company"], "description": j["description"][:8000],
                                    "posted": j.get("posted", ""), "source": j["source"]})

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
    save_state(full, sig)
    details.prune(tracker.load(TRACKER).keys())
    return 0


# ----------------------------------------------------------------------------- quick vs full search
def full_every() -> int:
    return int(PROFILE["search"].get("full_search_days", 7))


def _search_sig() -> str:
    """Changes when you change what is searched (phrases or countries): the next search is then a full one."""
    sp = PROFILE["search"]
    parts = [sp.get(k) for k in ("queries", "queries_other", "queries_oilgas", "queries_entry")] + [countries(PROFILE)]
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]


def _state() -> dict:
    try:
        return json.loads((ROOT / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def plan_search(force_full: bool = False) -> tuple[bool, str]:
    """Quick search when the last search finished recently and nothing about the search changed: ask the sites only for
    ads posted since then (+2 days margin). Otherwise, and every full_search_days, a full search."""
    st, sig, today = _state(), _search_sig(), date.today()

    def day(k):
        try:
            return date.fromisoformat(st.get(k, ""))
        except ValueError:
            return None
    last_ok, last_full = day("last_ok"), day("last_full")
    full = (force_full or not last_ok or not last_full or st.get("sig") != sig
            or (today - last_full).days >= full_every() or (today - last_ok).days > 10)
    util.WINDOW_DAYS = None if full else max(2, (today - last_ok).days + 2)
    return full, sig


def save_state(full: bool, sig: str) -> None:
    st = _state()
    st.update(last_ok=date.today().isoformat(), sig=sig)
    if full:
        st["last_full"] = date.today().isoformat()
    try:
        tracker.atomic_write(ROOT / "state.json", json.dumps(st, indent=1))
    except OSError:
        pass


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
