#!/usr/bin/env python3
"""Job Agent — local desktop app.

Double-click JobAgent.exe (or `python app.py`): a small local server starts and the control page opens in
your browser. All data lives in a `data` folder next to the program:
    data/profile.json   settings + CV facts        data/keys.json      optional API keys
    data/tracker.csv    every job found            data/applications/  one pack per match
    data/runs.log       history of searches

  JobAgent.exe            open the app
  JobAgent.exe --run      search once without opening the app (used by the daily scheduled task)
"""
from __future__ import annotations
import argparse, base64, hmac, io, json, os, re, secrets, shutil, smtplib, ssl, subprocess, sys, threading, time, webbrowser
from email.message import EmailMessage
from email.utils import formataddr
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

FROZEN = getattr(sys, "frozen", False)
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))      # read-only program files
HOME = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
DATA = HOME / "data"
PORT = 8765
TOKEN = secrets.token_urlsafe(24)       # per-launch secret written into the page; every /api call must carry it
PRIVATE_FILES = {"mail.json", "CV.pdf"}
PRIVATE_DIRS = {"browser", "browser-search", "sent"}


def ensure_data():
    from agent import tracker
    DATA.mkdir(exist_ok=True)
    (DATA / "applications").mkdir(exist_ok=True)
    for name in ("profile.json", "keys.json", "sources.json"):
        if not (DATA / name).exists():
            shutil.copy(BUNDLE / name, DATA / name)
    upgrade_config()
    if not (DATA / "tracker.csv").exists():
        tracker.write(DATA / "tracker.csv", {})
    else:
        try:                                         # one-time re-tag of rows from older versions (display tags only)
            with tracker.locked(DATA / "tracker.csv"):
                rows = tracker.load(DATA / "tracker.csv")
                profile = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
                if tracker.retag_old_rows(rows, profile):
                    tracker.write(DATA / "tracker.csv", rows)
        except Exception as e:  # noqa
            print(f"job list re-tag skipped: {e}")


# Rule sets the program owns: replaced when a newer program version ships new rules (_rules_version goes up).
# Your own facts and choices (candidate, form answers, evidence, countries, queries, minimum scores) are never changed.
RULE_KEYS = ("hard_reject_patterns", "title_reject_patterns", "title_penalties", "title_penalty_exempt", "sectors", "work_rights",
             "screening_answers", "target_titles", "keyword_points")
CATEGORY_RULE_KEYS = ("target_titles", "keyword_points", "requires_sector", "require_any", "label")


def _add_missing(dst: dict, src: dict) -> bool:
    changed = False
    for k, v in src.items():
        if k not in dst:
            dst[k] = v; changed = True
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            changed = _add_missing(dst[k], v) or changed
    return changed


def upgrade_config():
    """Bring the data folder's settings up to this program version - once per version, never undoing your own edits."""
    from agent import tracker
    try:
        user = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
        bundled = json.loads((BUNDLE / "profile.json").read_text(encoding="utf-8"))
        if int(user.get("_rules_version", 0)) < int(bundled.get("_rules_version", 0)):
            for k in RULE_KEYS:
                if k in bundled:
                    user[k] = bundled[k]
            ucat = user.setdefault("categories", {})
            for name, cat in bundled.get("categories", {}).items():
                if not isinstance(cat, dict):
                    ucat.setdefault(name, cat); continue
                mine = ucat.setdefault(name, {})
                for k in CATEGORY_RULE_KEYS:
                    if k in cat:
                        mine[k] = cat[k]
                    else:
                        mine.pop(k, None)
            us, bs = user.setdefault("search", {}), bundled.get("search", {})
            for k, v in bs.items():                          # new search settings and query lists for new sections
                if k not in us:
                    us[k] = v
            _add_missing(user, bundled)                      # brand-new sections (e.g. form_answers)
            user["_rules_version"] = bundled["_rules_version"]
            tracker.atomic_write(DATA / "profile.json", json.dumps(user, indent=2, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa
        print(f"profile upgrade skipped: {e}")
    try:
        user = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
        bundled = json.loads((BUNDLE / "sources.json").read_text(encoding="utf-8"))
        if int(user.get("_version", 0)) < int(bundled.get("_version", 0)):
            tracker.atomic_write(DATA / "sources.json", json.dumps(bundled, indent=2, ensure_ascii=False) + "\n")   # sources are program-owned
    except Exception as e:  # noqa
        print(f"sources upgrade skipped: {e}")


# ----------------------------------------------------------------------------- agent run
STATE = {"running": False, "log": [], "started": "", "finished": "", "summary": ""}
RUN_LOCK = "search.lock"


def _claim_run() -> bool:
    """One search at a time - across the app and the scheduled task. A lock older than 3 hours is from a crash."""
    lf = DATA / RUN_LOCK
    try:
        fd = os.open(lf, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - lf.stat().st_mtime > 3 * 3600:
                lf.unlink()
                return _claim_run()
        except OSError:
            pass
        return False
    os.write(fd, f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}".encode())
    os.close(fd)
    return True


def run_agent(block: bool = False, full: bool = False) -> bool:
    if STATE["running"] or not _claim_run():
        STATE["log"].append("A search is already running (in the app or the daily task).")
        return False

    def work():
        STATE.update(running=True, log=[], started=datetime.now().isoformat(timespec="seconds"), finished="", summary="")
        buf = io.StringIO()

        class Tee(io.TextIOBase):
            def write(self, s):
                buf.write(s)
                for line in s.splitlines():
                    if line.strip():
                        STATE["log"].append(line.strip()[:200])
                        del STATE["log"][:-200]
                return len(s)
        old_err, old_out = sys.stderr, sys.stdout
        sys.stderr = sys.stdout = Tee()
        try:
            import main as agent_main
            agent_main.ROOT = DATA
            agent_main.PROFILE = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
            agent_main.SOURCES = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
            agent_main.TRACKER = DATA / "tracker.csv"
            agent_main.REPORTS = DATA / "reports"
            agent_main.PACKS = DATA / "applications"
            import agent.sources as srcmod
            srcmod.KEYS_PATH = DATA / "keys.json"
            sys.argv = ["main.py"] + (["--full"] if full else [])
            agent_main.main()
        except BaseException as e:  # noqa - a broken settings file or anything else must not leave the app stuck "running"
            STATE["log"].append(f"ERROR: {type(e).__name__}: {e}")
            print(f"ERROR: {type(e).__name__}: {e}", file=buf)
        finally:
            try:
                from agent import sitebrowse
                sitebrowse.close()
            except Exception:
                pass
            sys.stderr, sys.stdout = old_err, old_out
            text = buf.getvalue()
            summ = next((l for l in text.splitlines() if l.startswith("===")), None)
            err = next((l for l in text.splitlines() if l.startswith("ERROR")), None)
            STATE.update(running=False, finished=datetime.now().isoformat(timespec="seconds"),
                         summary=(summ or err or "run finished").strip("= "))
            try:
                with (DATA / "runs.log").open("a", encoding="utf-8") as f:
                    f.write(f"{STATE['started']}\t{STATE['summary']}\n")
            except OSError:
                pass
            (DATA / RUN_LOCK).unlink(missing_ok=True)
    t = threading.Thread(target=work, daemon=True)
    t.start()
    if block:
        t.join()
    return True


# ----------------------------------------------------------------------------- Windows schedule
TASK = "JobAgentDaily"


def schedule(enable: bool, hour: int = 7) -> str:
    if os.name != "nt":
        return "Scheduling is available on Windows only."
    exe = sys.executable if FROZEN else f'"{sys.executable}" "{Path(__file__).resolve()}"'
    if enable:
        cmd = ["schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", TASK, "/ST", f"{hour:02d}:00", "/TR", f'"{exe}" --run' if FROZEN else f"{exe} --run"]
    else:
        cmd = ["schtasks", "/Delete", "/F", "/TN", TASK]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (r.stdout or r.stderr).strip()


def schedule_status() -> bool:
    if os.name != "nt":
        return False
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode == 0


# ----------------------------------------------------------------------------- e-mail applications
MAIL_FILE = "mail.json"
EMAIL_OK = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
PRESETS = {"gmail": ("smtp.gmail.com", 587), "outlook": ("smtp-mail.outlook.com", 587), "office365": ("smtp.office365.com", 587), "yahoo": ("smtp.mail.yahoo.com", 587)}


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI: encrypt/decrypt with the signed-in Windows user's key, so the mail password in mail.json
    is useless if the file is copied to another account or PC."""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    inp, out = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)):   # 0x1 = no UI
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def mail_settings() -> dict:
    try:
        m = json.loads((DATA / MAIL_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if m.get("password_dpapi"):
        try:
            m["password"] = _dpapi(base64.b64decode(m["password_dpapi"]), protect=False).decode("utf-8")
        except Exception:
            m["password"] = ""
    return m


def save_mail(m: dict):
    from agent import tracker
    m = dict(m)
    pw = m.pop("password", "")
    m.pop("password_dpapi", None)
    if pw:
        if os.name == "nt":
            m["password_dpapi"] = base64.b64encode(_dpapi(pw.encode("utf-8"), protect=True)).decode()
        else:
            m["password"] = pw           # non-Windows (development only)
    tracker.atomic_write(DATA / MAIL_FILE, json.dumps(m, indent=2))


def mail_public() -> dict:
    m = mail_settings()
    return {k: v for k, v in m.items() if k not in ("password", "password_dpapi")} | {"has_password": bool(m.get("password")), "has_cv": (DATA / "CV.pdf").exists()}


def smtp_connect(m: dict):
    host, port = PRESETS.get(m.get("preset", ""), (m.get("smtp_host", ""), int(m.get("smtp_port") or 587)))
    if not host:
        raise ValueError("No SMTP server set")
    if not m.get("user") or not m.get("password"):
        raise ValueError("E-mail address and app password are needed")
    s = smtplib.SMTP(host, port, timeout=30)
    s.ehlo(); s.starttls(context=ssl.create_default_context()); s.ehlo()
    s.login(m["user"], m["password"])
    return s


def tracker_rows() -> list[dict]:
    from agent import tracker
    return list(tracker.load(DATA / "tracker.csv").values())


def tracker_write(rows: list[dict], locked: bool = False):
    from agent import tracker
    if not rows:
        return
    data = {r["url"]: r for r in rows if r.get("url")}
    if locked:
        tracker.write(DATA / "tracker.csv", data)
    else:
        with tracker.LOCK:
            tracker.write(DATA / "tracker.csv", data)


def _pack_path(rel: str) -> Path | None:
    if not rel:
        return None
    p = (DATA / rel).resolve()
    root = (DATA / "applications").resolve()
    return p if p.is_relative_to(root) and p.suffix == ".md" and p.exists() else None


def draft_email(row: dict) -> dict:
    """Subject + body for one tracker row, from its application pack when there is one."""
    from agent.util import years_experience
    profile = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
    c = profile["candidate"]
    body = ""
    pack = _pack_path(row.get("pack_file", ""))
    if pack:
        md = pack.read_text(encoding="utf-8")
        m = re.search(r"## Cover letter\n+([\s\S]*?)(?=\n## |$)", md)
        if m:
            body = m.group(1).strip()
            body = re.sub(r"^\d{1,2} \w+ \d{4}\n+", "", body)   # the letter's date line is odd in an e-mail
    ai_letter = _details().load(row.get("url", "")).get("letter_ai", "")
    if ai_letter:
        body = ai_letter
    if not body:
        body = (f"Dear Hiring Manager,\n\nI would like to apply for the {row['title']} position"
                f"{' with ' + row['company'] if row.get('company') and row['company'] not in ('Unknown', 'Gulf Job Paper') else ''}. "
                f"I am a civil engineer with {years_experience(profile)} years of site and contract experience on water-network, roads and "
                f"airport infrastructure in Bahrain, NEBOSH IGC certified, available on {c['notice_period']}' notice and ready to relocate.\n\n"
                f"My CV is attached. I would welcome the chance to discuss the role.\n\nYours sincerely,\n{c['name']}\n{c['phone']} | {c['email']}")
    subject = f"Application for {row['title']} - {c['name']}"
    return {"to": row.get("apply_email", ""), "subject": subject, "body": body, "cv": "CV.pdf" if (DATA / "CV.pdf").exists() else ""}


def send_application(url: str, to: str, subject: str, body: str) -> dict:
    rcpt = [x.strip() for x in re.split(r"[,;\s]+", to) if x.strip()]
    if not rcpt or len(rcpt) > 3 or not all(EMAIL_OK.fullmatch(x) for x in rcpt):
        raise ValueError("Check the To address (up to 3 plain e-mail addresses)")
    m = mail_settings()
    if not (m.get("user") and m.get("password")):
        raise ValueError("E-mail settings are not complete (address + app password)")
    cv = DATA / "CV.pdf"
    if not cv.exists():
        raise ValueError("No CV attached yet - choose your CV PDF in the e-mail settings")
    if len(subject) > 200 or len(body) > 20000:
        raise ValueError("Subject or message is too long")
    msg = EmailMessage()
    msg["From"] = formataddr((m.get("from_name") or m["user"], m["user"]))
    msg["To"] = ", ".join(rcpt)
    if m.get("cc_self"):
        msg["Bcc"] = m["user"]
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(cv.read_bytes(), maintype="application", subtype="pdf", filename=m.get("cv_name") or "CV.pdf")
    with smtp_connect(m) as s:
        s.send_message(msg)
    sent = DATA / "sent"; sent.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe = "".join(ch if ch.isalnum() else "-" for ch in subject)[:60].strip("-")
    (sent / f"{stamp}_{safe}.eml").write_bytes(bytes(msg))     # your own copy; sent/ is never served to the page
    from agent import tracker
    with tracker.LOCK:
        rows = tracker_rows()
        for r in rows:
            if r.get("url") == url:
                r["status"] = "Applied"; r["applied_date"] = r.get("applied_date") or datetime.now().date().isoformat()
                r["user_notes"] = (r.get("user_notes", "") + f" | e-mailed {msg['To']} {stamp[:10]}").strip(" |")
        tracker_write(rows, locked=True)
    return {"ok": True, "to": msg["To"], "saved": f"sent/{stamp}_{safe}.eml"}


# ----------------------------------------------------------------------------- apply on site (browser helper)
APPLY: dict[str, object] = {}


def apply_on_site(url: str) -> dict:
    from agent import browser
    from agent.util import years_experience
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http(s) job links can be opened")
    row = next((r for r in tracker_rows() if r.get("url") == url), None)
    if not row:
        raise ValueError("job not in tracker")
    profile = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
    job = dict(row)
    try:
        job["cover_letter"] = draft_email(row)["body"]
    except Exception:
        job["cover_letter"] = ""
    old = APPLY.get(url)
    if old and not old.stop:
        return {"ok": True, "message": "already open in the apply window"}
    cv = DATA / "CV.pdf"
    sess = browser.ApplySession(url, profile, job, cv if cv.exists() else None, years_experience(profile),
                                on_submitted=lambda: mark_status(url, "Applied"), profile_dir=DATA / "browser")
    APPLY[url] = sess
    return {"ok": True, "message": "opened in the apply window", "cv": cv.exists()}


def mark_status(url: str, status: str) -> dict:
    from agent import tracker
    with tracker.LOCK:
        rows = tracker_rows()
        hit = False
        for r in rows:
            if r.get("url") == url:
                hit = True
                r["status"] = status
                if status == "Applied":
                    r["applied_date"] = r.get("applied_date") or datetime.now().date().isoformat()
        if hit:
            tracker_write(rows, locked=True)
    return {"ok": hit}


EDITABLE = {"status", "applied_date", "user_notes"}
STATUSES = {"New", "Review", "Applied", "Interview", "Offer", "Rejected", "Skipped", "Found (apply off)"}


def apply_edits(edits: dict) -> dict:
    """Status / note changes from the page, applied under the tracker lock so a running search can't overwrite them."""
    from agent import tracker
    if not isinstance(edits, dict):
        raise ValueError("bad edits")
    with tracker.LOCK:
        rows = tracker_rows()
        n = 0
        for r in rows:
            e = edits.get(r.get("url"))
            if not isinstance(e, dict):
                continue
            for k, v in e.items():
                if k in EDITABLE and isinstance(v, str) and len(v) <= 2000 and (k != "status" or v in STATUSES):
                    r[k] = v
                    n += 1
        tracker_write(rows, locked=True)
    return {"ok": True, "changed": n}


def _details():
    from agent import details
    details.ROOT = DATA / "details"
    return details


def _keys() -> dict:
    try:
        return json.loads((DATA / "keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ai_usage() -> int:
    from agent import ai
    ai.USAGE = DATA / "ai_usage.json"
    return ai._usage()


# ----------------------------------------------------------------------------- HTTP
MAX_BODY = 25 * 1024 * 1024          # the CV upload is the biggest request


class Handler(BaseHTTPRequestHandler):
    timeout = 60                      # a stalled client can't hold a thread forever

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code=200, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _safe(self, rel: str) -> Path | None:
        """Files the page may read/write: text files in the data folder, never the mail password, CV, sent mail or browser profile."""
        try:
            p = (DATA / rel).resolve()
        except (OSError, ValueError):
            return None
        root = DATA.resolve()
        if not p.is_relative_to(root) or p == root:
            return None
        parts = p.relative_to(root).parts
        if parts[0] in PRIVATE_DIRS or p.name in PRIVATE_FILES or p.suffix.lower() not in (".json", ".csv", ".md", ".txt", ".log"):
            return None
        return p

    def _trusted(self, api: bool) -> bool:
        """Only this app's own page may use the API: exact Host (DNS rebinding), same Origin (other websites),
        and for /api/* the per-launch token that is written into the page."""
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return False
        if api and not hmac.compare_digest(self.headers.get("X-JobAgent-Token", ""), TOKEN):
            return False
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("expected a JSON object")
        return d

    def do_OPTIONS(self):
        return self._send(403, b"")

    def do_GET(self):
        u = urlparse(self.path)
        if not self._trusted(api=u.path.startswith("/api/")):
            return self._json({"error": "forbidden"}, 403)
        try:
            self._get(u, parse_qs(u.query))
        except Exception as e:  # noqa - one bad request must not take the app down
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._trusted(api=True):
            return self._json({"error": "forbidden"}, 403)
        try:
            d = self._body()
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        try:
            self._post(u, parse_qs(u.query), d)
        except Exception as e:  # noqa
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _get(self, u, q):
        if u.path in ("/", "/index.html"):
            html = (BUNDLE / "panel_local.html").read_text(encoding="utf-8").replace("__JOBAGENT_TOKEN__", TOKEN)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/file":
            p = self._safe(q.get("path", [""])[0])
            if not p or not p.exists():
                return self._json({"error": "not found"}, 404)
            return self._json({"text": p.read_text(encoding="utf-8")})
        if u.path == "/api/status":
            runs = []
            if (DATA / "runs.log").exists():
                runs = [l.split("\t", 1) for l in (DATA / "runs.log").read_text(encoding="utf-8").splitlines()[-8:] if "\t" in l]
            return self._json({"running": STATE["running"], "log": STATE["log"][-6:], "summary": STATE["summary"], "started": STATE["started"],
                               "runs": [{"at": a, "summary": b} for a, b in reversed(runs)], "scheduled": schedule_status(), "data_dir": str(DATA)})
        if u.path == "/api/mail":
            return self._json(mail_public())
        if u.path == "/api/apply-status":
            return self._json({u_: {"stopped": s_.stop, "done": s_.done, "log": s_.log[-5:]} for u_, s_ in APPLY.items()})
        if u.path == "/api/job":
            url = q.get("url", [""])[0]
            row = next((r for r in tracker_rows() if r.get("url") == url), None)
            if not row:
                return self._json({"error": "job not in tracker"}, 404)
            d = _details().load(url)
            pack = _pack_path(row.get("pack_file", ""))
            return self._json({"description": d.get("description", ""), "ai": d.get("ai") or {}, "letter_ai": d.get("letter_ai", ""),
                               "posted": d.get("posted") or row.get("posted", ""),
                               "pack": pack.read_text(encoding="utf-8") if pack else ""})
        if u.path == "/api/ai":
            from agent import ai
            k = _keys()
            a = dict(k.get("ai") or {})
            if a.get("api_key"):
                a["api_key"] = "••••" + a["api_key"][-4:]
            return self._json({"settings": a, "active": bool(ai.settings(k)), "used_today": _ai_usage()})
        if u.path == "/api/draft":
            url = q.get("url", [""])[0]
            row = next((r for r in tracker_rows() if r.get("url") == url), None)
            if not row:
                return self._json({"error": "job not in tracker"}, 404)
            return self._json(draft_email(row))
        return self._json({"error": "no route"}, 404)

    def _post(self, u, q, d):
        from agent import tracker
        if u.path == "/api/file":
            p = self._safe(q.get("path", [""])[0])
            if not p:
                return self._json({"error": "bad path"}, 400)
            if p.name == "tracker.csv":
                return self._json({"error": "use /api/edits for the job list"}, 400)
            p.parent.mkdir(parents=True, exist_ok=True)
            text = d.get("text", "")
            if p.suffix == ".json":
                try:
                    json.loads(text)                 # never save a broken settings file
                except ValueError as e:
                    return self._json({"ok": False, "error": f"not saved - the settings text is not valid JSON ({e})"}, 400)
            tracker.atomic_write(p, text)
            return self._json({"ok": True})
        if u.path == "/api/edits":
            return self._json(apply_edits(d.get("edits", {})))
        if u.path == "/api/run":
            started = run_agent(full=bool(d.get("full")))
            return self._json({"ok": started, "error": "" if started else "A search is already running."})
        if u.path == "/api/schedule":
            hour = int(d.get("hour", 7))
            if not 0 <= hour <= 23:
                return self._json({"error": "hour must be 0-23"}, 400)
            return self._json({"ok": True, "message": schedule(bool(d.get("enable")), hour), "scheduled": schedule_status()})
        if u.path == "/api/mail":
            m = mail_settings()
            for k in ("preset", "smtp_host", "user", "from_name"):
                if k in d:
                    m[k] = str(d[k])[:200].strip()
            if "smtp_port" in d:
                m["smtp_port"] = int(d["smtp_port"] or 587)
            if "cc_self" in d:
                m["cc_self"] = bool(d["cc_self"])
            if d.get("password"):
                m["password"] = str(d["password"])
            save_mail(m)
            return self._json({"ok": True} | mail_public())
        if u.path == "/api/mail-test":
            try:
                with smtp_connect(mail_settings()):
                    pass
                return self._json({"ok": True, "message": "Signed in to the mail server - sending will work."})
            except Exception as e:
                return self._json({"ok": False, "message": f"{type(e).__name__}: {e}"})
        if u.path == "/api/cv":
            raw = base64.b64decode(d.get("data", ""), validate=True)
            if not raw.startswith(b"%PDF") or len(raw) > 15 * 1024 * 1024:
                return self._json({"ok": False, "error": "Please choose a PDF file under 15 MB"})
            tracker.atomic_write(DATA / "CV.pdf", raw)
            m = mail_settings()
            m["cv_name"] = "".join(ch for ch in str(d.get("name") or "CV.pdf") if ch.isalnum() or ch in " ._-()")[:80] or "CV.pdf"
            if not m["cv_name"].lower().endswith(".pdf"):
                m["cv_name"] += ".pdf"
            save_mail(m)
            return self._json({"ok": True, "name": m["cv_name"]})
        if u.path == "/api/send":
            try:
                return self._json(send_application(str(d["url"]), str(d["to"]), str(d["subject"]), str(d["body"])))
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/apply-site":
            try:
                return self._json(apply_on_site(str(d["url"])))
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/ai":
            from agent import ai
            k = _keys()
            a = k.setdefault("ai", {})
            for f in ("enabled", "provider", "model", "base_url", "max_per_run"):
                if f in d:
                    a[f] = d[f]
            if d.get("api_key"):
                a["api_key"] = str(d["api_key"]).strip()
            if a.get("provider") and a["provider"] not in ai.PROVIDERS:
                return self._json({"ok": False, "error": "unknown provider"}, 400)
            tracker.atomic_write(DATA / "keys.json", json.dumps(k, indent=2))
            return self._json({"ok": True})
        if u.path == "/api/ai-test":
            from agent import ai
            s_ = ai.settings(_keys())
            if not s_:
                return self._json({"ok": False, "message": "Turn the AI reviewer on and fill in the provider (and key for Groq) first."})
            try:
                return self._json({"ok": ai.test(s_) == "ok", "message": f"Connected to {s_['provider']} - model {s_['model']} answered."})
            except Exception as e:
                return self._json({"ok": False, "message": str(e)[:300]})
        if u.path == "/api/ai-letter":
            from agent import ai
            s_ = ai.settings(_keys())
            if not s_:
                return self._json({"ok": False, "error": "The AI reviewer is off - set it up in Settings."})
            url = str(d.get("url", ""))
            row = next((r for r in tracker_rows() if r.get("url") == url), None)
            det = _details().load(url)
            if not row or len(det.get("description", "")) < 150:
                return self._json({"ok": False, "error": "No ad text saved for this job yet - run a search first."})
            job = dict(row, description=det["description"])
            profile = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
            text = ai.letter(job, profile, s_)
            _details().save(url, {"letter_ai": text})
            return self._json({"ok": True, "letter": text})
        if u.path == "/api/open-folder":
            try:
                if os.name == "nt": os.startfile(DATA)  # type: ignore[attr-defined]
                else: subprocess.Popen(["xdg-open", str(DATA)])
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            return self._json({"ok": True})
        return self._json({"error": "no route"}, 404)


def _shutdown():
    try:
        from agent import browser
        browser.close_browser()
    except Exception:
        pass


def serve(open_browser=True):
    ensure_data()
    sys.path.insert(0, str(BUNDLE))
    import atexit
    atexit.register(_shutdown)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}/"
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(f"Job Agent running at {url}  (data in {DATA})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="search once and exit (used by the scheduled task)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    ensure_data()
    sys.path.insert(0, str(BUNDLE))
    if a.run:
        run_agent(block=True)
        print(STATE["summary"])
    else:
        try:
            serve(open_browser=not a.no_browser)
        except OSError:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")   # already running: just open it
