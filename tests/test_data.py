"""Country detection, duplicate keys, dates, and the job list's safety rules."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

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

    def test_reopen_after_rule_changes(self):
        rows = {"https://a": row("https://a", title="Civil Inspector", status="Skipped", desc_len="0",
                                 notes="auto: category:civil; title not in category")}
        self.assertEqual(tracker.reopen_after_rule_changes(rows, PROFILE), 1)
        self.assertEqual(rows["https://a"]["status"], "Review")


if __name__ == "__main__":
    unittest.main()


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
        self.assertEqual(tracker.merge_duplicates(rows), 2)
        self.assertEqual(set(rows), {"https://b"})
