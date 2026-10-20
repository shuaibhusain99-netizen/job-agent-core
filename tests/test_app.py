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


if __name__ == "__main__":
    unittest.main()
