"""Polite web access: sites that ask us to slow down are left alone, dead sites are skipped, and the browser-read job
boards page only as far as there is something new. No real website is contacted."""
import gzip
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agent import sitebrowse, util

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
os.environ["no_proxy"] = ",".join(x for x in (os.environ.get("no_proxy"), "127.0.0.1", "localhost") if x)


class Local(BaseHTTPRequestHandler):
    hits: dict = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        Local.hits[self.path] = Local.hits.get(self.path, 0) + 1
        if self.path == "/busy":
            self.send_response(429); self.send_header("Retry-After", "120"); self.end_headers(); return
        if self.path == "/down":
            self.send_response(503); self.end_headers(); return
        body = gzip.compress(("<p>ok " + "x" * 50 + "</p>").encode())
        self.send_response(200)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PoliteHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.port = s.getsockname()[1]; s.close()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", cls.port), Local)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        util._COOL.clear(); util._FAILS.clear(); util._DEAD.clear(); Local.hits.clear()

    def test_gzip_pages_are_read(self):
        st, body = util.http(self.base + "/page")
        self.assertEqual(st, 200)
        self.assertIn("ok xxx", body)

    def test_a_site_that_says_slow_down_is_left_alone(self):
        t0 = time.time()
        self.assertEqual(util.http(self.base + "/busy")[0], 429)
        self.assertLess(time.time() - t0, 10, "a long Retry-After must not be waited out")
        self.assertEqual(util.last_status(), 429)
        self.assertGreater(util._COOL["127.0.0.1"], time.time() + 590)
        self.assertEqual(util.http(self.base + "/page")[0], 429)          # the whole site rests, not just that page
        self.assertEqual(Local.hits.get("/page"), None)
        self.assertTrue(util.host_blocked(self.base + "/anything"))

    def test_a_dead_site_is_skipped_after_three_failures(self):
        for _ in range(2):
            self.assertEqual(util.http(self.base + "/down", retries=0)[0], 503)
        self.assertNotIn("127.0.0.1", util._DEAD)
        util.http(self.base + "/down", retries=0)
        self.assertIn("127.0.0.1", util._DEAD)
        self.assertEqual(util.http(self.base + "/page"), (0, ""))
        self.assertEqual(Local.hits.get("/page"), None)

    def test_cool_downs_are_remembered_between_searches(self):
        with tempfile.TemporaryDirectory() as d:
            util.COOLDOWN_FILE = Path(d) / "cooldown.json"
            try:
                util._COOL.update({"busy.example": time.time() + 3600, "old.example": time.time() - 5})
                util.save_cooldowns()
                util._COOL.clear()
                util.load_cooldowns()
                self.assertIn("busy.example", util._COOL)
                self.assertNotIn("old.example", util._COOL)
                self.assertEqual(util.http("https://busy.example/jobs"), (429, ""))
                util.COOLDOWN_FILE.write_text("{broken", encoding="utf-8")
                util.load_cooldowns()                                       # a damaged file is ignored, not fatal
                self.assertEqual(util._COOL, {})
            finally:
                util.COOLDOWN_FILE = None

    def test_only_web_links_are_fetched(self):
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x.example/a"):
            self.assertEqual(util.http(bad), (0, ""))

    def test_sites_are_grouped_for_pauses(self):
        self.assertEqual(util.site_key("https://sa.linkedin.com/jobs/view/1"), "linkedin.com")
        self.assertEqual(util.site_key("https://www.linkedin.com/jobs-guest/x"), "linkedin.com")
        self.assertEqual(util.site_key("https://www.seek.com.au/job/1"), "seek.com.au")
        self.assertEqual(util.site_key("https://www.jobbank.gc.ca/x"), "jobbank.gc.ca")


class FakeTab:
    """Stands in for a browser tab: every list page returns `per_page` jobs; the URLs it was sent to are recorded."""
    def __init__(self, per_page=10, blocked=False):
        self.urls, self.per_page, self.blocked, self.closed = [], per_page, blocked, False

    def call(self, method, timeout=None, **kw):
        if method == "Page.navigate":
            self.urls.append(kw["url"]); return {}
        expr = kw.get("expression", "")
        if expr == "document.readyState":
            return {"result": {"value": "complete"}}
        if expr == sitebrowse.CHALLENGE_JS:
            return {"result": {"value": self.blocked}}
        page = len(self.urls)
        if self.blocked:
            return {"result": {"value": []}}                                # a "verify you are human" page has no job list
        rows = [{"title": "Civil Engineer", "company": f"Co {page}-{i}", "location": "Riyadh",
                 "url": f"https://sa.indeed.com/viewjob?jk=p{page}x{i}", "age": "2 days ago"} for i in range(self.per_page)]
        return {"result": {"value": rows}}

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, tab):
        self._tab, self.dropped = tab, 0

    def tab(self):
        return self._tab

    def drop_tab(self):
        self.dropped += 1


def gulf_profile(*names):
    p = json.loads(json.dumps(PROFILE))
    p["search"]["countries"] = {n: {"weight": 1.0, "search": n in names, "apply": True} for n in ("Saudi Arabia", "Qatar")}
    return p


class BrowserBoards(unittest.TestCase):
    def setUp(self):
        self.known, util.KNOWN = util.KNOWN, set()
        self.sleep = mock.patch("agent.sitebrowse.time.sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        util.KNOWN = self.known
        sitebrowse.BLOCKED.clear()

    def test_indeed_searches_phrases_in_bracketed_groups_one_page_each(self):
        tab = FakeTab()
        jobs = sitebrowse._read_site("indeed", {"queries": ["civil engineer", "site engineer", "quantity surveyor", "hse officer"]},
                                     gulf_profile("Saudi Arabia"), FakeSession(tab))
        self.assertEqual(len(tab.urls), 2)                                  # 3 phrases per search; page 2 needs a sign-in
        self.assertIn("q=%28%22civil+engineer%22+OR+%22site+engineer%22+OR+%22quantity+surveyor%22%29", tab.urls[0])
        self.assertIn("sort=date", tab.urls[0])
        self.assertNotIn("start=", " ".join(tab.urls))
        self.assertEqual(len(jobs), 20)
        self.assertEqual(jobs[0]["country"], "Saudi Arabia")
        self.assertEqual(jobs[0]["posted"], (date.today().fromordinal(date.today().toordinal() - 2)).isoformat())

    def test_paging_stops_when_a_page_holds_only_jobs_already_in_the_list(self):
        util.KNOWN = {f"indeed:p2x{i}" for i in range(10)}                 # page 2 was all seen in an earlier search
        tab = FakeTab()
        sitebrowse._read_site("naukrigulf", {"queries": ["civil engineer"], "pages": 6}, gulf_profile("Saudi Arabia"), FakeSession(tab))
        self.assertEqual(len(tab.urls), 2)

    def test_a_short_page_is_the_last_page(self):
        tab = FakeTab(per_page=4)
        sitebrowse._read_site("bayt", {"queries": ["civil engineer"], "pages": 3}, gulf_profile("Saudi Arabia"), FakeSession(tab))
        self.assertEqual(len(tab.urls), 1)

    def test_a_human_check_skips_the_site_without_retrying(self):
        tab, sess = FakeTab(blocked=True), None
        sess = FakeSession(tab)
        jobs = sitebrowse._read_site("bayt", {"queries": ["civil engineer", "site engineer"], "pages": 2}, gulf_profile("Saudi Arabia", "Qatar"), sess)
        self.assertEqual((jobs, len(tab.urls)), ([], 1))
        self.assertIn("Bayt", sitebrowse.BLOCKED)
        self.assertEqual(sess.dropped, 1)                                   # its tab is closed

    def test_a_site_showing_no_job_list_is_given_up_quickly(self):
        tab = FakeTab(per_page=0)                                           # an error page, or the site changed
        jobs = sitebrowse._read_site("naukrigulf", {"queries": ["civil engineer", "site engineer", "quantity surveyor"], "pages": 2},
                                     gulf_profile("Saudi Arabia", "Qatar"), FakeSession(tab))
        self.assertEqual((jobs, len(tab.urls)), ([], 3))
        self.assertIn("NaukriGulf", sitebrowse.BLOCKED)                    # its full ads aren't tried either

    def test_switched_off_boards_are_not_opened(self):
        self.assertEqual(sitebrowse.fetch({"enabled": False, "sites": {"indeed": True}}, PROFILE), [])
        self.assertEqual(sitebrowse.fetch({"enabled": True, "sites": {"indeed": False, "bayt": False}}, PROFILE), [])


class Logs(unittest.TestCase):
    def test_keys_never_reach_the_log(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            util.log("  network error https://sa.jooble.org/api/deadbeef-aaaa-bbbb: timed out")
            util.log("GET https://x.example/search?q=a&api_key=SECRET123&page=2")
        out = buf.getvalue()
        self.assertNotIn("deadbeef", out)
        self.assertNotIn("SECRET123", out)
        self.assertIn("jooble.org/api/<key>", out)


class Placeholders(unittest.TestCase):
    def test_answers_follow_your_details(self):
        from agent.tailor import answer_for
        p = json.loads(json.dumps(PROFILE))
        p["candidate"]["notice_period"] = "1 month"
        self.assertIn("1 month", answer_for("What is your notice period?", p))
        self.assertNotIn("{", answer_for("How many years of experience do you have?", p))
        p["candidate"]["location"] = "Manama, Bahrain"
        self.assertIn("Manama, Bahrain", answer_for("Where are you currently based?", p))

    def test_internal_location_codes_read_as_places(self):
        from agent.tailor import _place
        self.assertEqual(_place({"location": "CA.ON.Toronto.191 The West Mall", "country": "Canada"}), "Toronto, Canada")
        self.assertEqual(_place({"location": "AE - Dubai", "country": "United Arab Emirates"}), "Dubai, United Arab Emirates")
        self.assertEqual(_place({"location": "Riyadh, Riyadh, Saudi Arabia", "country": "Saudi Arabia"}), "Riyadh, Saudi Arabia")


if __name__ == "__main__":
    unittest.main()
