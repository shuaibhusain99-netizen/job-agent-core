"""Country detection, duplicate keys, dates, and the job list's safety rules."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from agent import tracker
from agent.util import guess_country, dedupe_key, safe_url
from agent.sitebrowse import parse_age

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))


class Countries(unittest.TestCase):
    def test_places(self):
        cases = {
            "Riyadh, Saudi Arabia": "Saudi Arabia", "Riyadh, SA": "Saudi Arabia", "SA.Riyadh": "Saudi Arabia", "Riyadh, SAU": "Saudi Arabia",
            "AE - Abu Dhabi": "United Arab Emirates", "Dubai": "United Arab Emirates", "Doha Metropolitan Area": "Qatar",
            "Muscat, OM": "Oman", "Kuwait City": "Kuwait", "Adelaide, SA": "Australia", "Sydney NSW": "Australia",
            "Toronto, ON": "Canada", "Houston, TX": "Other", "Cairo, Egypt": "Other", "UZ": "Other", "": "",
            "GCC": "", "Middle East": "", "Multiple locations": "", "Victoria, BC": "Canada", "Melbourne, Victoria": "Australia",
            "Mussafah": "United Arab Emirates", "Rabigh": "Saudi Arabia", "AEDubai": "United Arab Emirates", "Perth WA": "Australia",
            "Darwin NT": "Australia", "Riyadh 12345": "Saudi Arabia", "Khalifa City, Abu Dhabi": "United Arab Emirates",
            "Al Ahsa": "Saudi Arabia", "London, ON": "Canada", "London, UK": "Other", "KSA": "Saudi Arabia", "Salalah": "Oman",
            "Brisbane City QLD 4000": "Australia", "CA.ON.Pickering.1340 Pickering Parkway": "Canada", "AU.NSW.Sydney": "Australia",
        }
        for loc, want in cases.items():
            self.assertEqual(guess_country(loc), want, loc)


class Keys(unittest.TestCase):
    def k(self, url):
        return dedupe_key({"url": url, "title": "", "company": "", "location": ""})

    def test_linkedin_subdomains_are_one_job(self):
        self.assertEqual(self.k("https://sa.linkedin.com/jobs/view/civil-engineer-at-x-4477461739?trk=a"),
                         self.k("https://www.linkedin.com/jobs/view/4477461739"))

    def test_indeed_ids_in_the_query_string_are_kept(self):
        self.assertNotEqual(self.k("https://sa.indeed.com/viewjob?jk=aaa"), self.k("https://sa.indeed.com/viewjob?jk=bbb"))
        self.assertEqual(self.k("https://sa.indeed.com/viewjob?jk=AAA"), self.k("https://ae.indeed.com/viewjob?jk=aaa&from=x"))

    def test_tracking_parameters_are_ignored(self):
        self.assertEqual(self.k("https://jobs.x.com/job/123/?utm_source=a"), self.k("https://jobs.x.com/job/123"))

    def test_unsafe_urls(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "file:///C:/Windows", "//evil", ""):
            self.assertFalse(safe_url(bad), bad)
        self.assertTrue(safe_url("https://ok.example/job"))


class Dates(unittest.TestCase):
    def test_relative_and_absolute(self):
        today = date(2026, 10, 20)
        self.assertEqual(parse_age("2 days ago", today), "2026-10-18")
        self.assertEqual(parse_age("Just posted", today), "2026-10-20")
        self.assertEqual(parse_age("Yesterday", today), "2026-10-19")
        self.assertEqual(parse_age("30+ days ago", today), "2026-09-20")
        self.assertEqual(parse_age("9 Jun", today), "2026-06-09")
        self.assertEqual(parse_age("29 Dec", today), "2025-12-29")          # a date "in the future" is last year's
        self.assertEqual(parse_age("October 19, 2026", today), "2026-10-19")
        self.assertEqual(parse_age("", today), "")


def row(url, **kw):
    r = {k: "" for k in tracker.FIELDS}
    r.update(found_date=date.today().isoformat(), score="70", verdict="apply", title="Civil Engineer", company="X",
             country="Saudi Arabia", source="LinkedIn", url=url, status="New", notes="category:civil")
    r.update(kw)
    return r


class JobList(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "tracker.csv"

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip_and_formula_escaping(self):
        rows = {"https://a": row("https://a", title="=HYPERLINK(\"http://evil\")", user_notes="+1 call")}
        tracker.write(self.path, rows)
        raw = self.path.read_text(encoding="utf-8-sig")
        self.assertIn("'=HYPERLINK", raw)
        back = tracker.load(self.path)["https://a"]
        self.assertEqual(back["title"], "=HYPERLINK(\"http://evil\")")
        self.assertEqual(back["user_notes"], "+1 call")

    def test_your_edits_during_a_search_win(self):
        tracker.write(self.path, {"https://a": row("https://a")})
        search_copy = tracker.load(self.path)                 # the search loads the list ...
        app_copy = tracker.load(self.path)                    # ... you change it in the app meanwhile
        app_copy["https://a"].update(status="Applied", user_notes="sent CV", touched=date.today().isoformat())
        tracker.write(self.path, app_copy)
        search_copy["https://a"]["score"] = "80"              # the search re-scored it
        tracker.save(self.path, search_copy)
        final = tracker.load(self.path)["https://a"]
        self.assertEqual(final["status"], "Applied")
        self.assertEqual(final["user_notes"], "sent CV")

    def test_prune_keeps_what_you_acted_on(self):
        old = (date.today() - timedelta(days=200)).isoformat()
        rows = {u: row(u, found_date=old, status=s, user_notes=n) for u, s, n in
                [("https://new", "New", ""), ("https://applied", "Applied", ""), ("https://noted", "Skipped", "keep"),
                 ("https://skipped", "Skipped", "")]}
        tracker.prune(rows)
        self.assertEqual(set(rows), {"https://applied", "https://noted"})

    def test_agent_skipped_rows_are_judged_again(self):
        auto = row("https://a", status="Skipped", notes="auto: category:civil; title not in category")
        mine = row("https://b", status="Skipped", notes="auto: category:civil; title not in category", touched="2026-10-01")
        self.assertTrue(tracker.auto_skipped(auto))
        self.assertFalse(tracker.auto_skipped(mine))
        j = {"match_score": 66, "verdict": "apply", "reasons": "category:site_support; target title", "category": "site_support",
             "sector": "", "description": "x" * 500, "level": "support", "country": "Saudi Arabia"}
        self.assertTrue(tracker.refresh(auto, j, ["Saudi Arabia"]))
        self.assertEqual(auto["status"], "New")
        self.assertEqual(tracker.refresh(mine, dict(j), ["Saudi Arabia"]), "")
        self.assertEqual(mine["status"], "Skipped")

    def test_a_snippet_never_overrides_a_full_ad_verdict(self):
        r = row("https://a", desc_len="3000", score="70")
        j = {"match_score": 10, "verdict": "reject", "reasons": "x", "description": "short"}
        tracker.refresh(r, j, ["Saudi Arabia"])
        self.assertEqual(r["status"], "New")

    def test_excel_resaved_files_are_still_read(self):
        tracker.write(self.path, {"https://a": row("https://a", found_date="2026-10-20")})
        good = self.path.read_text(encoding="utf-8-sig")
        # Excel "CSV (Comma delimited)" in a ';' locale with dd/mm/yyyy dates and an ANSI encoding
        bad = good.replace(",", ";").replace("2026-10-20", "20/10/2026").replace("Civil Engineer", "Ingénieur civil")
        self.path.write_bytes(bad.encode("cp1252"))
        r = tracker.load(self.path)["https://a"]
        self.assertEqual((r["found_date"], r["title"]), ("2026-10-20", "Ingénieur civil"))

    def test_an_unreadable_list_is_never_overwritten(self):
        self.path.write_text("this is not the job list\n1,2,3\n", encoding="utf-8")
        with self.assertRaises(tracker.Unreadable):
            tracker.save(self.path, {"https://a": row("https://a")})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "this is not the job list\n1,2,3\n")

    def test_results_wait_when_the_list_is_open_in_excel(self):
        tracker.write(self.path, {"https://a": row("https://a")})
        real = tracker.atomic_write
        def locked_by_excel(path, data, encoding="utf-8"):
            if Path(path).name == "tracker.csv":
                raise PermissionError("open in Excel")
            return real(path, data, encoding)
        tracker.atomic_write = locked_by_excel
        try:
            tracker.save(self.path, {"https://a": row("https://a"), "https://b": row("https://b")})
        finally:
            tracker.atomic_write = real
        self.assertTrue(tracker.pending_path(self.path).exists())
        with tracker.locked(self.path):
            rows = tracker.load_current(self.path)
        self.assertEqual(set(rows), {"https://a", "https://b"})
        self.assertFalse(tracker.pending_path(self.path).exists())

    def excel_has_the_list(self):
        real = tracker.atomic_write

        def locked_by_excel(path, data, encoding="utf-8"):
            if Path(path).name == "tracker.csv":
                raise PermissionError("open in Excel")
            return real(path, data, encoding)
        return mock.patch.object(tracker, "atomic_write", locked_by_excel)

    def test_waiting_results_are_used_while_excel_still_has_the_list(self):
        tracker.write(self.path, {"https://a": row("https://a")})
        with self.excel_has_the_list():
            tracker.save(self.path, {"https://a": row("https://a"), "https://b": row("https://b")})
            with tracker.locked(self.path):
                rows = tracker.load_current(self.path)
            self.assertEqual(set(rows), {"https://a", "https://b"})      # the new job is there (and its saved ad is kept)
            rows["https://c"] = row("https://c")
            tracker.save(self.path, rows)                                 # a second search before Excel is closed
        with tracker.locked(self.path):
            self.assertEqual(set(tracker.load_current(self.path)), {"https://a", "https://b", "https://c"})
        self.assertFalse(tracker.pending_path(self.path).exists())

    def test_an_edit_made_in_excel_after_the_search_wins_and_the_searchs_own_changes_stay(self):
        tracker.write(self.path, {"https://a": row("https://a"), "https://s": row("https://s")})
        with self.excel_has_the_list():
            rows = tracker.load(self.path)
            rows["https://s"]["status"] = "Skipped"                      # the search skipped this one
            tracker.save(self.path, rows)
        d = tracker.load(self.path)                                       # meanwhile in Excel: you mark "a" Applied and save
        d["https://a"]["status"] = "Applied"
        tracker.write(self.path, d)
        with tracker.locked(self.path):
            rows = tracker.load_current(self.path)
        self.assertEqual((rows["https://a"]["status"], rows["https://s"]["status"]), ("Applied", "Skipped"))

    def test_a_duplicate_you_acted_on_during_the_search_is_kept(self):
        li, sk = "https://www.linkedin.com/jobs/view/123456", "https://www.seek.com.au/job/9"
        tracker.write(self.path, {li: row(li), sk: row(sk)})
        rows = tracker.load(self.path)
        rows.pop(li)                                                      # the search merged it into the SEEK copy
        d = tracker.load(self.path)
        d[li].update(status="Applied", touched=date.today().isoformat())  # you applied through it meanwhile
        tracker.write(self.path, d)
        tracker.save(self.path, rows, dropped=[li])
        self.assertEqual(tracker.load(self.path)[li]["status"], "Applied")
        rows = tracker.load(self.path)
        tracker.write(self.path, {li: row(li), sk: row(sk)})
        rows = tracker.load(self.path); rows.pop(li)
        tracker.save(self.path, rows, dropped=[li])                       # untouched: it stays merged away
        self.assertNotIn(li, tracker.load(self.path))

    def test_rules_update_does_not_count_as_seen(self):
        old = (date.today() - timedelta(days=50)).isoformat()
        ad = "Site engineer for civil works, roads and drainage. 3-5 years experience. " * 6
        rows = {"https://x": row("https://x", last_seen=old, desc_len=str(len(ad)))}
        tracker.rescore_with_new_rules(rows, PROFILE, lambda u: {"description": ad}, ["Saudi Arabia"])
        self.assertEqual(rows["https://x"]["last_seen"], old)

    def test_your_own_review_or_skip_is_kept(self):
        mine = row("https://a", status="Review", touched="2026-10-20")
        j = {"match_score": 90, "verdict": "apply", "reasons": "x", "category": "civil", "sector": "", "description": "y" * 900}
        tracker.refresh(mine, j, ["Saudi Arabia"])
        self.assertEqual(mine["status"], "Review")
        old = (date.today() - timedelta(days=400)).isoformat()
        rows = {"https://s": row("https://s", status="Skipped", found_date=old, touched=old)}
        tracker.prune(rows)
        self.assertIn("https://s", rows)

    def test_a_job_still_listed_is_not_pruned(self):
        old = (date.today() - timedelta(days=60)).isoformat()
        rows = {"https://a": row("https://a", found_date=old, last_seen=date.today().isoformat())}
        tracker.prune(rows)
        self.assertIn("https://a", rows)

    def test_reopen_after_rule_changes(self):
        rows = {"https://a": row("https://a", title="Civil Inspector", status="Skipped", desc_len="0",
                                 notes="auto: category:civil; title not in category")}
        self.assertEqual(tracker.reopen_after_rule_changes(rows, PROFILE), 1)
        self.assertEqual(rows["https://a"]["status"], "Review")


if __name__ == "__main__":
    unittest.main()


class RulesUpdate(unittest.TestCase):
    def test_jobs_whose_ad_was_not_saved_wait_in_review_when_a_limit_applies(self):
        rows = {"https://au": row("https://au", country="Australia", desc_len="2500"),
                "https://sr": row("https://sr", level="senior"),
                "https://ok": row("https://ok"),
                "https://mine": row("https://mine", country="Australia", touched="2026-10-01")}
        n = tracker.rescore_with_new_rules(rows, PROFILE, lambda u: {}, ["Australia", "Saudi Arabia"])
        self.assertEqual(n, 2)
        self.assertEqual((rows["https://au"]["status"], rows["https://au"]["desc_len"]), ("Review", "0"))
        self.assertTrue(rows["https://au"]["notes"].startswith("needs a work visa"))
        self.assertEqual(rows["https://sr"]["status"], "Review")
        self.assertEqual(rows["https://ok"]["status"], "New")
        self.assertEqual(rows["https://mine"]["status"], "New")          # one you acted on is never re-judged

    def test_an_unknown_country_is_not_a_country_switched_off(self):
        r = row("https://x", country="", status="Review", desc_len="0")
        j = {"title": "Civil Engineer", "company": "X", "country": "", "description": "x" * 500, "match_score": 80, "verdict": "apply",
             "reasons": "category:civil", "category": "civil"}
        tracker.refresh(r, j, ["Saudi Arabia"])
        self.assertEqual(r["status"], "New")
        r = row("https://y", country="Bahrain", status="Review", desc_len="0")
        tracker.refresh(r, dict(j, country="Bahrain"), ["Saudi Arabia"])
        self.assertEqual(r["status"], "Found (apply off)")

    def test_matches_kept_aside_move_to_to_apply_when_applying_is_switched_on(self):
        rows = {"https://q": row("https://q", country="Qatar", status="Found (apply off)"),
                "https://b": row("https://b", country="Bahrain", status="Found (apply off)"),
                "https://u": row("https://u", country="", status="Found (apply off)"),
                "https://n": row("https://n", country="Qatar", status="Found (apply off)", user_notes="not this one")}
        self.assertEqual(tracker.release_found(rows, ["Qatar"]), 2)
        self.assertEqual([rows[u]["status"] for u in ("https://q", "https://b", "https://u", "https://n")],
                         ["New", "Found (apply off)", "New", "Found (apply off)"])

    def test_saved_ads_are_judged_again(self):
        ad = "Site engineer for civil works, roads and drainage. 3-5 years experience. " * 6
        rows = {"https://au": row("https://au", country="Australia", location="Brisbane QLD", title="Site Engineer", desc_len=str(len(ad)))}
        tracker.rescore_with_new_rules(rows, PROFILE, lambda u: {"description": ad}, ["Australia"])
        self.assertEqual(rows["https://au"]["status"], "Review")
        ad2 = ad + " Visa sponsorship available for the right candidate."
        rows = {"https://au": row("https://au", country="Australia", location="Brisbane QLD", title="Site Engineer", desc_len=str(len(ad2)))}
        tracker.rescore_with_new_rules(rows, PROFILE, lambda u: {"description": ad2}, ["Australia"])
        self.assertEqual(rows["https://au"]["status"], "New")


class Duplicates(unittest.TestCase):
    def test_company_name_variants_are_one_job(self):
        from agent.util import same_job_key
        a = {"title": "Senior Civil Engineer", "company": "Parsons Corporation", "country": "Saudi Arabia"}
        b = {"title": "Senior  Civil Engineer", "company": "PARSONS", "country": "Saudi Arabia"}
        c = {"title": "Senior Civil Engineer", "company": "Parsons", "country": "Qatar"}
        self.assertEqual(same_job_key(a), same_job_key(b))
        self.assertNotEqual(same_job_key(a), same_job_key(c))
        self.assertEqual(same_job_key({"title": "x", "company": "Unknown", "country": "Qatar"}), "")

    def test_merge_keeps_the_row_you_acted_on(self):
        rows = {"https://a": row("https://a", company="Parsons", status="New", desc_len="0"),
                "https://b": row("https://b", company="Parsons Corporation", status="Applied", touched="2026-10-01"),
                "https://c": row("https://c", company="Parsons Corp", status="Review", desc_len="4000")}
        self.assertEqual(len(tracker.merge_duplicates(rows)), 2)
        self.assertEqual(set(rows), {"https://b"})

    def test_two_ads_on_the_same_site_are_two_openings(self):
        rows = {"https://www.linkedin.com/jobs/view/111": row("https://www.linkedin.com/jobs/view/111", company="ACCIONA"),
                "https://www.linkedin.com/jobs/view/222": row("https://www.linkedin.com/jobs/view/222", company="ACCIONA")}
        self.assertEqual(tracker.merge_duplicates(rows), [])
        self.assertEqual(len(rows), 2)

    def test_different_cities_are_different_jobs(self):
        from agent.util import same_job_key
        a = {"title": "Civil Engineer", "company": "Mott MacDonald", "country": "Australia", "location": "Sydney NSW"}
        b = dict(a, location="Perth, Western Australia")
        self.assertNotEqual(same_job_key(a), same_job_key(b))
