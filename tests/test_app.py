"""The local app's API: only its own page (token + exact Host + same Origin) may use it, and private files stay private."""
import http.client
import json
import shutil
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import app


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class AppAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        app.DATA = Path(cls.tmp) / "data"
        app.PORT = free_port()
        app.ensure_data()
        (app.DATA / "mail.json").write_text('{"user": "me@example.com", "password": "secret"}', encoding="utf-8")
        from agent import tracker
        tracker.write(app.DATA / "tracker.csv", {"https://job/1": {"url": "https://job/1", "title": "Civil Engineer", "status": "New",
                                                                  "found_date": "2026-10-20", "score": "70"}})
        cls.srv = ThreadingHTTPServer(("127.0.0.1", app.PORT), app.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, token=True, host=None, origin=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", app.PORT, timeout=10)
        h = {"Host": host or f"127.0.0.1:{app.PORT}", "Content-Type": "application/json"}
        if token:
            h["X-JobAgent-Token"] = app.TOKEN
        if origin:
            h["Origin"] = origin
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def test_page_has_token_and_security_headers(self):
        st, h, body = self.req("GET", "/", token=False)
        self.assertEqual(st, 200)
        self.assertIn(app.TOKEN.encode(), body)
        self.assertIn("frame-ancestors 'none'", h.get("Content-Security-Policy", ""))
        self.assertNotIn("Access-Control-Allow-Origin", h)

    def test_api_needs_the_token(self):
        self.assertEqual(self.req("GET", "/api/status", token=False)[0], 403)
        self.assertEqual(self.req("POST", "/api/run", {}, token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/status")[0], 200)

    def test_dns_rebinding_and_other_sites_are_refused(self):
        self.assertEqual(self.req("GET", "/", token=False, host=f"evil.example:{app.PORT}")[0], 403)
        self.assertEqual(self.req("GET", "/api/status", origin="https://evil.example")[0], 403)
        self.assertEqual(self.req("OPTIONS", "/api/status")[0], 403)

    def test_private_files_and_traversal(self):
        from urllib.parse import quote
        for p in ("mail.json", "../app.py", "..%2Fapp.py", "browser/Local State", "sent/x.eml", "CV.pdf", "/etc/passwd"):
            st, _, _ = self.req("GET", "/api/file?path=" + quote(p, safe="%/"))
            self.assertIn(st, (403, 404), p)
        self.assertEqual(self.req("POST", "/api/file?path=tracker.csv", {"text": "x"})[0], 400)
        self.assertEqual(self.req("POST", "/api/file?path=profile.json", {"text": "{broken"})[0], 400)

    def test_edits_are_checked_and_recorded(self):
        st, _, body = self.req("POST", "/api/edits", {"edits": {"https://job/1": {"status": "Applied", "title": "hacked"}}})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["changed"], 1)
        st, _, body = self.req("POST", "/api/edits", {"edits": {"https://job/1": {"status": "Nonsense"}}})
        self.assertEqual(json.loads(body)["changed"], 0)
        from agent import tracker
        r = tracker.load(app.DATA / "tracker.csv")["https://job/1"]
        self.assertEqual((r["status"], r["title"]), ("Applied", "Civil Engineer"))
        self.assertTrue(r["touched"])

    def test_oversized_and_malformed_bodies(self):
        st, _, _ = self.req("POST", "/api/edits", raw=b"[1,2,3]")
        self.assertEqual(st, 400)
        c = http.client.HTTPConnection("127.0.0.1", app.PORT, timeout=10)
        c.putrequest("POST", "/api/edits", skip_host=True)
        c.putheader("Host", f"127.0.0.1:{app.PORT}")
        c.putheader("X-JobAgent-Token", app.TOKEN)
        c.putheader("Content-Length", str(app.MAX_BODY + 1))
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()


    def test_only_the_apps_own_script_runs_on_the_page(self):
        import re
        st, h, body = self.req("GET", "/", token=False)
        html = body.decode("utf-8")
        nonce = re.search(r"'nonce-([^']+)'", h["Content-Security-Policy"]).group(1)
        self.assertEqual(html.count("<script"), 1)
        self.assertIn(f'<script nonce="{nonce}">', html)
        self.assertIn("object-src 'none'", h["Content-Security-Policy"])
        self.assertIsNone(re.search(r"<[^>]+\son[a-z]+\s*=", html), "inline event handlers would be blocked by the CSP")
        nonce2 = re.search(r"'nonce-([^']+)'", self.req("GET", "/", token=False)[1]["Content-Security-Policy"]).group(1)
        self.assertNotEqual(nonce, nonce2)

    def test_keys_are_never_sent_to_the_page(self):
        self.assertEqual(self.req("GET", "/api/file?path=keys.json")[0], 404)
        st, _, body = self.req("POST", "/api/keys", {"jooble": {"sa": "abcd-1234-secret"}})
        self.assertEqual((st, json.loads(body)["ok"]), (200, True))
        st, _, body = self.req("GET", "/api/keys")
        self.assertNotIn(b"abcd-1234-secret", body)
        self.assertIs(json.loads(body)["jooble"]["sa"], True)
        self.assertIs(json.loads(body)["jooble"]["ae"], False)
        for bad in ({"jooble": {"zz": "abc"}}, {"jooble": {"sa": "abc def"}}, {"jooble": {"sa": "x" * 101}}):
            self.assertEqual(self.req("POST", "/api/keys", bad)[0], 400, bad)
        st, _, body = self.req("POST", "/api/keys", {"ai": {"api_key": "x", "base_url": "http://evil.example"}})
        self.assertNotIn("evil", (app.DATA / "keys.json").read_text(encoding="utf-8"))

    def test_ai_settings_are_checked_and_the_key_stays_hidden(self):
        st, _, body = self.req("POST", "/api/ai", {"provider": "groq", "api_key": "gsk_abcdefgh1234", "enabled": True, "max_per_run": 50})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["why_off"], "")
        st, _, body = self.req("GET", "/api/ai")
        self.assertNotIn(b"gsk_abcdefgh", body)
        self.assertTrue(json.loads(body)["settings"]["api_key"].endswith("1234"))
        for bad in ({"provider": "evil"}, {"model": "a b"}, {"max_per_run": "lots"}, {"api_key": "with space"}):
            self.assertEqual(self.req("POST", "/api/ai", bad)[0], 400, bad)

    def test_mail_password_only_goes_to_the_server_it_was_saved_for(self):
        st, _, body = self.req("POST", "/api/mail", {"user": "other@example.com"})
        self.assertFalse(json.loads(body)["has_password"])
        self.assertNotIn(b"secret", self.req("GET", "/api/mail")[2])
        self.assertEqual(self.req("POST", "/api/mail", {"user": "not an address"})[0], 400)
        self.assertEqual(self.req("POST", "/api/mail", {"smtp_port": 70000})[0], 400)
        self.assertEqual(self.req("POST", "/api/mail", {"smtp_host": "evil host; rm"})[0], 400)

    def test_status_reports_the_daily_search(self):
        st, _, body = self.req("GET", "/api/status")
        s = json.loads(body)
        self.assertTrue(0 <= s["schedule_hour"] <= 23)
        self.assertFalse(s["elsewhere"])
        lock = app.DATA / app.RUN_LOCK
        lock.write_text("123", encoding="utf-8")
        try:
            s = json.loads(self.req("GET", "/api/status")[2])
            self.assertTrue(s["running"] and s["elsewhere"])
            self.assertEqual(json.loads(self.req("POST", "/api/run", {})[2])["ok"], False)     # never two searches at once
        finally:
            lock.unlink()
        self.assertEqual(self.req("POST", "/api/schedule", {"enable": False, "hour": 25})[0], 400)
        self.assertEqual(self.req("POST", "/api/schedule", {"enable": False, "hour": "x"})[0], 400)

    def test_edit_of_a_job_no_longer_listed_says_so(self):
        st, _, body = self.req("POST", "/api/edits", {"edits": {"https://gone/1": {"status": "Skipped"}}})
        self.assertFalse(json.loads(body)["ok"])

    def test_the_page_gets_the_list_even_after_excel_resaved_it(self):
        path = app.DATA / "tracker.csv"
        before = path.read_bytes()
        try:
            path.write_bytes("url;title;status;found_date;score\r\nhttps://job/1;Ingénieur – civil;New;20/10/2026;70\r\n".encode("cp1252"))
            st, _, body = self.req("GET", "/api/file?path=tracker.csv")
            self.assertEqual(st, 200)
            import csv, io
            rows = list(csv.DictReader(io.StringIO(json.loads(body)["text"])))
            self.assertEqual((rows[0]["title"], rows[0]["found_date"]), ("Ingénieur – civil", "2026-10-20"))
            path.write_text("this is not a job list\n", encoding="utf-8")
            st, _, body = self.req("GET", "/api/file?path=tracker.csv")
            self.assertEqual(st, 500)
            self.assertIn("can't be read", json.loads(body)["error"])
        finally:
            path.write_bytes(before)

    def test_quit_waits_for_a_running_search(self):
        app.STATE["running"] = True
        try:
            st, _, body = self.req("POST", "/api/quit", {})
            self.assertFalse(json.loads(body)["ok"])
            self.assertIn("search is running", json.loads(body)["error"])
        finally:
            app.STATE["running"] = False
        self.assertEqual(self.req("POST", "/api/quit", {}, token=False)[0], 403)      # another site can't stop the app

    def test_email_a_company_you_name(self):
        from unittest import mock
        st, _, body = self.req("POST", "/api/manual-draft", {"company": "Nesma & Partners", "position": "Site Engineer", "to": "hr@nesma.example"})
        d = json.loads(body)
        self.assertTrue(d["ok"], d)
        self.assertIn("Site Engineer", d["subject"])
        self.assertIn("Nesma & Partners", d["body"])
        self.assertNotIn("{", d["body"])
        self.assertFalse(json.loads(self.req("POST", "/api/manual-draft", {"company": "", "position": "Site Engineer", "to": "x@y.com"})[2])["ok"])
        sent = []

        class FakeSMTP:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def send_message(self, msg): sent.append(msg)
        (app.DATA / "CV.pdf").write_bytes(b"%PDF-1.4 test")
        before = (app.DATA / "mail.json").read_text(encoding="utf-8")
        (app.DATA / "mail.json").write_text('{"user": "me@example.com", "password": "pw", "preset": "gmail"}', encoding="utf-8")
        try:
            with mock.patch.object(app, "smtp_connect", lambda m: FakeSMTP()):
                bad = json.loads(self.req("POST", "/api/send-manual", {"company": "Nesma", "position": "Site Engineer", "to": "not-an-address",
                                                                       "subject": "s", "body": "b"})[2])
                self.assertFalse(bad["ok"])
                r = json.loads(self.req("POST", "/api/send-manual", {"company": "Nesma", "position": "Site Engineer", "to": "hr@nesma.example",
                                                                     "subject": d["subject"], "body": d["body"], "country": "Saudi Arabia"})[2])
            self.assertTrue(r["ok"], r)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["To"], "hr@nesma.example")
            self.assertEqual(sent[0].get_payload()[1].get_content_type(), "application/pdf")       # the CV goes with it
            from agent import tracker
            row = tracker.load(app.DATA / "tracker.csv")[r["url"]]
            self.assertEqual((row["status"], row["company"], row["title"], row["country"]), ("Applied", "Nesma", "Site Engineer", "Saudi Arabia"))
        finally:
            (app.DATA / "mail.json").write_text(before, encoding="utf-8")
            (app.DATA / "CV.pdf").unlink(missing_ok=True)

    def test_a_refused_sign_in_is_explained_and_nothing_is_tried_for_outlook(self):
        import smtplib
        from unittest import mock
        (app.DATA / "CV.pdf").write_bytes(b"%PDF-1.4 test")
        before = (app.DATA / "mail.json").read_text(encoding="utf-8")
        tried = []
        try:
            (app.DATA / "mail.json").write_text('{"user": "me@outlook.com", "password": "pw", "preset": "outlook"}', encoding="utf-8")
            pub = json.loads(self.req("GET", "/api/mail")[2])
            self.assertIn("Microsoft switched it off", pub["blocked"])
            with mock.patch.object(app.smtplib, "SMTP", lambda *a, **k: tried.append(a)):
                r = json.loads(self.req("POST", "/api/send-manual", {"company": "Nesma", "position": "Site Engineer", "to": "hr@nesma.example",
                                                                     "subject": "s", "body": "b"})[2])
                t = json.loads(self.req("POST", "/api/mail-test", {})[2])
            self.assertEqual(tried, [])                                   # no doomed sign-in, so no Microsoft security alert
            self.assertFalse(r["ok"]); self.assertTrue(r["settings"])
            self.assertIn("Gmail", r["error"]); self.assertIn("Nothing was sent", r["error"])
            self.assertNotIn("SMTPAuthenticationError", r["error"] + t["message"])
            self.assertEqual(json.loads(self.req("GET", "/api/mail")[2])["signin"], "failed")
            # Gmail with a wrong password: told to use an app password
            (app.DATA / "mail.json").write_text('{"user": "me@gmail.com", "password": "pw", "preset": "gmail"}', encoding="utf-8")
            self.assertEqual(json.loads(self.req("GET", "/api/mail")[2])["blocked"], "")
            def refuse(m): raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
            with mock.patch.object(app, "smtp_connect", refuse):
                t = json.loads(self.req("POST", "/api/mail-test", {})[2])
            self.assertIn("app password", t["message"])
            # changing the settings forgets the old result
            self.req("POST", "/api/mail", {"user": "other@gmail.com", "password": "abcd efgh ijkl mnop"})
            self.assertNotIn("signin", json.loads(self.req("GET", "/api/mail")[2]))
            self.assertIn("internet", app.mail_error(OSError("timed out"), {"preset": "gmail"})[0])
        finally:
            (app.DATA / "mail.json").write_text(before, encoding="utf-8")
            (app.DATA / "CV.pdf").unlink(missing_ok=True)

    def test_page_only_reads_and_writes_its_own_files(self):
        for name in ("tracker.csv", "profile.json", "sources.json"):
            self.assertEqual(self.req("GET", "/api/file?path=" + name)[0], 200, name)
        for name in ("keys.json", "mail.json", "cooldown.json", "state.json", "seen.json", "Profile.json"):
            self.assertEqual(self.req("GET", "/api/file?path=" + name)[0], 404, name)
        for name in ("keys.json", "runs.log", "tracker.csv", "mail.json"):
            self.assertEqual(self.req("POST", "/api/file?path=" + name, {"text": "{}"})[0], 400, name)

if __name__ == "__main__":
    unittest.main()
