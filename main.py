#!/usr/bin/env python3
"""Job Agent — search, match, prepare, track. Run by the desktop app (app.py) with ROOT etc. pointed at its data folder.

Run:  python main.py            (daily run: only what's new since the last search; a full search every few days)
      python main.py --full     (full search)
      python main.py --dry      (no job-list write; prints results)
Needs only the Python standard library.
"""
from __future__ import annotations
import argparse, csv, hashlib, json, sys
from datetime import date, timedelta
from pathlib import Path

from agent import sources as sources_mod
from agent.sources import fetch_all, enrich_many, load_keys
from agent import sitebrowse
from agent.score import score
from agent.tailor import application_pack
from agent import tracker, ai, details
from agent.util import dedupe_key, log, countries, site_key
from agent import util

ROOT = Path(__file__).resolve().parent
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
TRACKER = ROOT / "tracker.csv"
REPORTS = ROOT / "reports"
PACKS = ROOT / "applications"

CATEGORY_NAMES = {"civil"} | {k for k in PROFILE.get("categories", {}) if not k.startswith("_")}
WINDOWED = ("linkedin_guest", "seek", "browser_sites")      # sources asked only for ads since their last good search


def say(text: str) -> None:
    """Print, even where the console can't show Arabic or other characters."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def main() -> int:
    global CATEGORY_NAMES
    CATEGORY_NAMES = {"civil"} | {k for k in PROFILE.get("categories", {}) if not k.startswith("_")}
    details.ROOT = ROOT / "details"
    sitebrowse.PROFILE_DIR = ROOT / "browser-search"
    util.COOLDOWN_FILE = ROOT / "cooldown.json"
    util.load_cooldowns()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="don't write the job list")
    ap.add_argument("--max-enrich", type=int, default=300, help="how many jobs get their full ad fetched (best first)")
    ap.add_argument("--full", action="store_true", help="search the full period, not only what's new since the last search")
    args = ap.parse_args()
    REPORTS.mkdir(exist_ok=True)
    PACKS.mkdir(exist_ok=True)
    if PROFILE["search"].get("paused"):
        say("Agent is paused (profile.json search.paused = true). Nothing to do.")
        return 0
    try:
        return _run(args)
    finally:
        sitebrowse.close()
        util.save_cooldowns()


def _run(args) -> int:
    # 1. search - only what's new since the last search, with a full search every few days
    with tracker.locked(TRACKER):
        known = tracker.load_current(TRACKER)       # includes results kept aside while the list was open in Excel
    seen_before = _load_seen()
    util.KNOWN = {dedupe_key(r) for r in known.values()} | {util.same_job_key(r) for r in known.values()} | set(seen_before)
    util.KNOWN.discard("")
    st = _state()
    full, sig = plan_search(args.full, st)
    log("Full search (all recent ads)" if not util.WINDOW_DAYS else
        f"Quick search: ads from the last {util.WINDOW_DAYS} days (a full search runs every {full_every()} days)")
    raw = fetch_all(SOURCES, PROFILE)
    counts = dict(sources_mod.LAST_COUNTS)
    unique: dict[str, dict] = {}
    by_tcc: dict[str, str] = {}
    for j in raw:
        k = dedupe_key(j)
        if not k:
            continue
        # the same title at the same company (and city) in the same country, found on two different sites, is one job:
        # keep the first one seen, but take the fuller ad text from the copy that has it (no need to fetch it again).
        # Two different ads on the SAME site are two openings.
        tcc = util.same_job_key(j)
        first = unique.get(k)
        if first is None and tcc in by_tcc and site_key(unique[by_tcc[tcc]]["url"]) != site_key(j["url"]):
            first = unique[by_tcc[tcc]]
        if first is None:
            unique[k] = j
            if tcc:
                by_tcc.setdefault(tcc, k)
        else:
            if len(j.get("description", "")) > len(first.get("description", "")) and not j.get("_snippet"):
                first["description"] = j["description"]
                first.pop("_snippet", None)
            if not first.get("posted") and j.get("posted"):
                first["posted"] = j["posted"]
    log(f"Collected {len(raw)} rows, {len(unique)} unique")

    # 2. the job list as it stands
    apply_countries = countries(PROFILE, "apply")
    tracker.backfill_from_notes(known)
    n = tracker.retag_old_rows(known, PROFILE)
    if n:
        log(f"Re-tagged {n} rows saved by an older version")
    if st.get("rules_version") != PROFILE.get("_rules_version"):
        n = tracker.rescore_with_new_rules(known, PROFILE, details.load, apply_countries)
        if n:
            log(f"Matching rules were updated: {n} jobs in your list were judged again on their saved ad")
    n = tracker.reopen_after_rule_changes(known, PROFILE, details.load)
    if n:
        log(f"Re-opened {n} jobs that older rules had skipped (they're in Review)")
    n = tracker.release_found(known, apply_countries)
    if n:
        log(f"{n} matches moved to To apply (applying is on for their country now)")
    packs_of = {u: r.get("pack_file", "") for u, r in known.items()}
    merged = tracker.merge_duplicates(known)
    for u in merged:
        util.KNOWN.discard(u)
    if merged:
        log(f"Merged {len(merged)} duplicate jobs (the same ad from two sites)")
    known_key = {dedupe_key(r): u for u, r in known.items()}
    known_tcc = {}
    for u, r in known.items():                      # the same job saved earlier from another site counts as known too
        sk = util.same_job_key(r)
        if sk:
            known_tcc.setdefault(sk, u)
    tracker.sweep_titles(known, PROFILE)          # packs of archived rows are removed only after saving (you may act meanwhile)

    # 3. score on what the listing gives, then fetch the full ad for promising jobs and re-score.
    #    Known jobs are re-read only if they are still open in your list and were judged on a snippet.
    jobs = list(unique.values())
    claimed: set[str] = set()
    for j in jobs:
        j["match_score"], j["verdict"], r = score(j, PROFILE)
        j["reasons"] = "; ".join(r)
    jobs.sort(key=lambda j: -j["match_score"])
    for j in jobs:
        u = known_key.get(dedupe_key(j))
        if not u:
            t = known_tcc.get(util.same_job_key(j) or "-")
            if t and site_key(t) != site_key(j["url"]):
                u = t                               # the same ad, saved earlier from another site
        if u and u in claimed and dedupe_key(known[u]) != dedupe_key(j):
            u = None                                # that row already matched another listing today: this one is new
        if u:
            claimed.add(u)
        j["_known"] = u
        j["_key"] = u or j["url"]                   # saved ad text, AI review and retry notes live under the row's link
    want = []
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    for j in jobs:
        if j["verdict"] == "reject":
            continue
        if j["_known"]:
            row = known[j["_known"]]
            if not tracker.rejudgeable(row) or tracker._known_len(row) >= 400:
                continue
            if details.load(j["_key"]).get("tried", "") >= week_ago:
                continue                        # its full ad couldn't be read in the last week: don't ask again daily
        want.append(j)
    try:
        fetched = enrich_many(want, args.max_enrich)
    finally:
        sitebrowse.close()                      # the search browser is not needed after this
    for j in want:
        if j.get("_tried") and not j.get("_rate_limited") and (len(j.get("description", "")) < 300 or j.get("_snippet")):
            details.save(j["_key"], {"tried": date.today().isoformat()})
        if len(j.get("description", "")) >= 300:
            j["match_score"], j["verdict"], r = score(j, PROFILE)
            j["reasons"] = "; ".join(r)
    jobs.sort(key=lambda j: -j["match_score"])
    log(f"Fetched full descriptions for {fetched} jobs")

    # 3b. AI reviewer (optional): reads the promising ads like a recruiter and has the final say on fit
    keys = load_keys()
    if ai.settings(keys):
        ai.USAGE = ROOT / "ai_usage.json"
        cand = [j for j in jobs if j["verdict"] != "reject" and (not j["_known"] or tracker.rejudgeable(known[j["_known"]]))]
        ai.review_many(cand, PROFILE, keys, cache_get=lambda u: details.load(u).get("ai") or {},
                       cache_put=lambda u, r: details.save(u, {"ai": r}))
        for j in cand:
            r = j.get("ai")
            if not r:
                continue
            cat = r["role_family"] if r["role_family"] in CATEGORY_NAMES else j.get("category")
            sp = PROFILE["search"]
            thr = sp["min_score"] if cat == "civil" else (sp.get("min_score_oilgas", sp["min_score"]) if cat == "oil_gas" else sp.get("min_score_other", sp["min_score"] + 5))
            j["match_score"], j["verdict"] = ai.combine(j["match_score"], j["verdict"], r, thr, j.get("_cap", ""))
            j["category"] = cat
            j["level"] = r["level"] or j.get("level", "")
            j["reasons"] = (f"AI {r['fit']}: {r['summary']}" + (f" | deal-breakers: {'; '.join(r['deal_breakers'])}" if r['deal_breakers'] else "")
                            + " || rules: " + j["reasons"])
        jobs.sort(key=lambda j: -j["match_score"])

    # keep the ad text for the app's detail view
    for j in jobs:
        if j["verdict"] != "reject" and len(j.get("description", "")) >= 80:
            details.save(j["_key"], {"title": j["title"], "company": j["company"], "description": j["description"][:8000],
                                     "posted": j.get("posted", ""), "source": j["source"]})

    # 4. update known jobs, add new ones, write application packs for matches
    new_rows, updated, skipped = [], 0, 0
    for j in jobs:
        if j["_known"]:
            row = known[j["_known"]]
            res = tracker.refresh(row, j, apply_countries)
            updated += res == "updated"
            if res == "skipped":
                skipped += 1
            elif row.get("status") == "New" and not row.get("pack_file"):
                row["pack_file"] = write_pack(dict(j, url=row["url"]))      # re-judged as a match: it needs its letter too
            continue
        if j["verdict"] == "reject":
            continue
        if j["verdict"] == "apply" and j.get("country") and j["country"] not in apply_countries:
            j["verdict"] = "found"          # country searched but applying switched off (an unknown country isn't "off")
        if j["verdict"] == "apply":
            j["pack_file"] = write_pack(j)
        row = tracker.row_from_job(j)
        new_rows.append(row)
        known[j["url"]] = row
    for r in known.values():                # rows re-opened or re-judged above without being listed today
        if r.get("status") == "New" and not r.get("pack_file") and tracker.rejudgeable(r):
            d = details.load(r["url"])
            j = {"title": r.get("title", ""), "company": r.get("company", ""), "location": r.get("location", ""),
                 "country": r.get("country", ""), "description": d.get("description", ""), "source": r.get("source", ""),
                 "url": r["url"], "category": r.get("category", "civil"), "match_score": r.get("score", ""),
                 "verdict": r.get("verdict", "apply"), "reasons": r.get("notes", "")}
            r["pack_file"] = write_pack(j)

    # how to apply, for LinkedIn jobs saved before the agent recorded it (To apply first, best matches first)
    if not args.dry:
        from agent.sources import check_linkedin_apply
        todo = [r for r in known.values() if r.get("source") == "LinkedIn" and not r.get("apply_how") and not r.get("apply_email")
                and r.get("status") in tracker.OPEN]
        todo.sort(key=lambda r: (r.get("status") != "New", -tracker._int(r.get("score"))))
        try:
            check_linkedin_apply(todo, 40)
        except Exception as e:  # noqa - only a convenience
            log(f"LinkedIn apply check stopped: {type(e).__name__}: {e}")

    # 5. report (a copy you can open in Excel; old ones are cleared after 30 days)
    stamp = date.today().isoformat()
    write_report(stamp, jobs)
    apply_new = [r for r in new_rows if r["verdict"] == "apply"]
    log(f"Jobs already in your list: {updated} updated, {skipped} moved to Archived by today's rules")
    say(f"\n=== {stamp}: {len(jobs)} jobs read, {len(new_rows)} new, {len(apply_new)} new in To apply ===")
    for r in sorted(new_rows, key=lambda r: -tracker._int(r["score"]))[:25]:
        say(f"[{tracker._int(r['score']):>3}] {r['verdict']:<6} {r['title']} — {r['company']} ({r['location']}) [{r['source']}]")
        say(f"       {r['url']}")
        if r.get("pack_file"):
            say(f"       pack: {r['pack_file']}")

    if args.dry:
        say("\n(dry run: job list not updated)")
        return 0
    for r in tracker.save(TRACKER, known, dropped=merged):
        drop_pack(r)
    with tracker.locked(TRACKER):
        saved = tracker.load_current(TRACKER)
    for u in merged:                                # duplicates merged away: their packs go too (unless you kept the row)
        if u not in saved and packs_of.get(u):
            drop_pack({"pack_file": packs_of[u]})
    save_state(full, sig, counts, len(raw))
    _save_seen(seen_before, unique)
    details.prune(saved.keys())
    return 0


def write_pack(j: dict) -> str:
    safe = "".join(c if c.isalnum() else "-" for c in f"{j['company']}-{j['title']}")[:60].strip("-")
    tag = hashlib.sha1(j["url"].encode()).hexdigest()[:6]
    pack = PACKS / f"{date.today():%Y-%m-%d}-{safe}-{tag}.md"
    try:
        pack.write_text(application_pack(j, PROFILE), encoding="utf-8")
    except OSError as e:
        log(f"  couldn't write the application pack for {j['title'][:40]}: {e}")
        return ""
    return pack.relative_to(ROOT).as_posix()


def write_report(stamp: str, jobs: list[dict]) -> None:
    try:
        cut = (date.today() - timedelta(days=30)).isoformat()
        for old in REPORTS.glob("jobs_*.csv"):
            if old.stem[5:] < cut:
                old.unlink(missing_ok=True)
        with (REPORTS / f"jobs_{stamp}.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["match_score", "verdict", "title", "company", "location", "country", "source", "url", "reasons"],
                               extrasaction="ignore")
            w.writeheader()
            for j in jobs:
                if j["verdict"] != "reject":
                    w.writerow({k: tracker._esc(j.get(k, "")) for k in w.fieldnames})
    except OSError as e:                     # e.g. today's report is open in Excel: the search itself still counts
        log(f"Today's report file couldn't be written ({e}) - the job list is still updated")


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
        st = json.loads((ROOT / "state.json").read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _day(v):
    try:
        return date.fromisoformat(str(v or ""))
    except (TypeError, ValueError):
        return None


def plan_search(force_full: bool = False, st: dict | None = None) -> tuple[bool, str]:
    """Quick search when the last search finished recently and nothing about the search changed: ask the sites only for
    ads posted since then (+2 days margin) - per source, so a source that failed for a few days looks back further.
    Otherwise, and every full_search_days, a full search."""
    st = _state() if st is None else st
    sig, today = _search_sig(), date.today()
    last_ok, last_full = _day(st.get("last_ok")), _day(st.get("last_full"))
    full = (force_full or not last_ok or not last_full or st.get("sig") != sig
            or (today - last_full).days >= full_every() or (today - last_ok).days > 10)
    util.WINDOW_DAYS = None if full else max(2, (today - last_ok).days + 2)
    util.SOURCE_WINDOWS = {}
    src_ok = st.get("src_ok") if isinstance(st.get("src_ok"), dict) else {}
    for key in WINDOWED:
        d = _day(src_ok.get(key))
        util.SOURCE_WINDOWS[key] = None if (full or not d or (today - d).days > 10) else max(2, (today - d).days + 2)
    return full, sig


def save_state(full: bool, sig: str, counts: dict | None = None, found: int = 0) -> None:
    """Only a search that actually got results moves the dates on - an offline or blocked run changes nothing."""
    st = _state()
    today = date.today().isoformat()
    src_ok = st.get("src_ok") if isinstance(st.get("src_ok"), dict) else {}
    for key, n in (counts or {}).items():
        if n:
            src_ok[key] = today
    st["src_ok"] = src_ok
    st["rules_version"] = PROFILE.get("_rules_version")
    if found:
        st.update(last_ok=today, sig=sig)
        if full and found >= 50:
            st["last_full"] = today
    try:
        tracker.atomic_write(ROOT / "state.json", json.dumps(st, indent=1))
    except OSError:
        pass


def _load_seen() -> dict:
    """Every job any search has listed in the last 30 days (also the ones judged not relevant, which aren't in your
    list): lets paging stop when a page holds nothing new."""
    try:
        d = json.loads((ROOT / "seen.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_seen(seen: dict, unique: dict) -> None:
    today = date.today().isoformat()
    cut = (date.today() - timedelta(days=30)).isoformat()
    keep = {k: v for k, v in seen.items() if isinstance(v, str) and v >= cut}
    keep.update({k: today for k in unique})
    keep.update({util.same_job_key(j): today for j in unique.values() if util.same_job_key(j)})
    try:
        tracker.atomic_write(ROOT / "seen.json", json.dumps(keep, separators=(",", ":")))
    except OSError:
        pass


def drop_pack(row: dict) -> None:
    """Delete the application pack of a job the rules have archived (only files inside the packs folder)."""
    rel = row.get("pack_file") or ""
    if not rel:
        return
    try:
        p = (ROOT / rel).resolve()
        if p.is_relative_to(PACKS.resolve()) and p.suffix == ".md":
            p.unlink(missing_ok=True)
    except OSError:
        return                                  # open in an editor: leave it, it's harmless
    row["pack_file"] = ""


if __name__ == "__main__":
    sys.exit(main())
