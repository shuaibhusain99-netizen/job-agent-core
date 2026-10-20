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
import argparse, base64, csv, io, json, os, shutil, smtplib, ssl, subprocess, sys, threading, time, webbrowser
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
PUBLIC_PATH = "/api/mark"
PRIVATE_FILES = {"mail.json", "CV.pdf"}
PRIVATE_DIRS = {"browser", "sent"}


def ensure_data():
    DATA.mkdir(exist_ok=True)
    (DATA / "applications").mkdir(exist_ok=True)
    for name in ("profile.json", "keys.json", "sources.json"):
        if not (DATA / name).exists():
            shutil.copy(BUNDLE / name, DATA / name)
    upgrade_config()
    if not (DATA / "tracker.csv").exists():
        (DATA / "tracker.csv").write_text("﻿found_date,score,verdict,title,company,location,country,source,url,status,applied_date,notes,pack_file,user_notes,category,sector,salary,apply_email\n", encoding="utf-8")


def _merge(dst, src, path=""):
    """Add keys/items a newer program version introduced, without touching the user's own settings."""
    changed = False
    if isinstance(dst, dict) and isinstance(src, dict):
        for k, v in src.items():
            if k not in dst:
                dst[k] = v; changed = True
            elif isinstance(v, (dict, list)) and isinstance(dst[k], type(v)):
                changed = _merge(dst[k], v, path + "/" + k) or changed
    elif isinstance(dst, list) and isinstance(src, list):
        if all(isinstance(x, str) for x in src):                      # word lists: union
            have = {x.lower() for x in dst if isinstance(x, str)}
            for x in src:
                if x.lower() not in have:
                    dst.append(x); changed = True
        elif all(isinstance(x, dict) for x in src):                   # boards / sites: add new ones by name
            key = lambda d: (d.get("company") or d.get("name") or d.get("account") or json.dumps(d, sort_keys=True))
            have = {key(d) for d in dst if isinstance(d, dict)}
            for d in src:
                if key(d) not in have:
                    dst.append(d); changed = True
    return changed


def upgrade_config():
    """profile.json / sources.json in the data folder gain whatever the bundled copies added (new sources, categories ...)."""
    for name in ("profile.json", "sources.json"):
        try:
            user = json.loads((DATA / name).read_text(encoding="utf-8"))
            bundled = json.loads((BUNDLE / name).read_text(encoding="utf-8"))
            changed = _merge(user, bundled)
            if name == "profile.json":                       # settings this version retired
                for k in ("fresh graduate", "oil & gas", "oil and gas"):
                    if k in user.get("title_penalties", {}):
                        user["title_penalties"].pop(k); changed = True
                if user.get("title_penalties", {}).get("intern", 0) < -15:
                    user["title_penalties"]["intern"] = -15; changed = True
                retired = {r"\b(1[0-9]|[2-9][0-9])\+?\s*(years|yrs)", r"\b(7|8|9)\s*(-|to|–)\s*(1[0-9]|[2-9][0-9])\s*(years|yrs)",
                           r"\bminimum\s+(7|8|9|1[0-9])\s*(years|yrs)", r"\b(7|8|9)\+\s*(years|yrs)", r"\bsaudi\s+nationals?\s+only\b",
                           r"\bfor\s+saudis\s+only\b", r"\b(bachelor|degree)\s+in\s+(electrical|mechanical|chemical|computer)\s+engineering\b"}
                hr = user.get("hard_reject_patterns", [])
                if any(x in retired for x in hr):
                    user["hard_reject_patterns"] = [x for x in hr if x not in retired]; changed = True
                for c in user.get("search", {}).get("countries", {}).values():
                    if isinstance(c, dict) and c.get("weight", 1.0) != 1.0:
                        c["weight"] = 1.0; changed = True
            if changed:
                (DATA / name).write_text(json.dumps(user, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception as e:  # noqa
            print(f"config upgrade skipped for {name}: {e}")
    # older tracker files: add the new columns
    try:
        t = DATA / "tracker.csv"
        head = t.read_text(encoding="utf-8-sig").splitlines()[0] if t.exists() else ""
        missing = [c for c in ("sector", "salary", "apply_email") if head and c not in head.split(",")]
        if missing:
            import csv, io
            rows = list(csv.DictReader(io.StringIO(t.read_text(encoding="utf-8-sig"))))
            fields = head.split(",") + missing
            with t.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
    except Exception as e:  # noqa
        print(f"tracker upgrade skipped: {e}")


# ----------------------------------------------------------------------------- agent run
STATE = {"running": False, "log": [], "started": "", "finished": "", "summary": ""}


def run_agent(block: bool = False):
    if STATE["running"]:
        return
    def work():
        STATE.update(running=True, log=[], started=datetime.now().isoformat(timespec="seconds"), finished="", summary="")
        import main as agent_main
        agent_main.ROOT = DATA
        agent_main.PROFILE = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
        agent_main.SOURCES = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
        agent_main.TRACKER = DATA / "tracker.csv"
        agent_main.REPORTS = DATA / "reports"
        agent_main.PACKS = DATA / "applications"
        import agent.sources as srcmod
        srcmod.KEYS_PATH = DATA / "keys.json"
        buf = io.StringIO()
        class Tee(io.TextIOBase):
            def write(self, s):
                buf.write(s)
                for line in s.splitlines():
                    if line.strip():
                        STATE["log"].append(line.strip()[:200])
                return len(s)
        old_err, old_out = sys.stderr, sys.stdout
        sys.stderr = sys.stdout = Tee()
        try:
            sys.argv = ["main.py"]
            agent_main.main()
        except Exception as e:  # noqa
            STATE["log"].append(f"ERROR: {e}")
        finally:
            sys.stderr, sys.stdout = old_err, old_out
        text = buf.getvalue()
        summ = next((l for l in text.splitlines() if l.startswith("===")), "run finished")
        STATE.update(running=False, finished=datetime.now().isoformat(timespec="seconds"), summary=summ.strip("= "))
        with (DATA / "runs.log").open("a", encoding="utf-8") as f:
            f.write(f"{STATE['started']}\t{STATE['summary']}\n")
    t = threading.Thread(target=work, daemon=True)
    t.start()
    if block:
        t.join()


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
PRESETS = {"gmail": ("smtp.gmail.com", 587), "outlook": ("smtp-mail.outlook.com", 587), "office365": ("smtp.office365.com", 587), "yahoo": ("smtp.mail.yahoo.com", 587)}


def mail_settings() -> dict:
    try:
        return json.loads((DATA / MAIL_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def mail_public() -> dict:
    m = mail_settings()
    return {k: v for k, v in m.items() if k != "password"} | {"has_password": bool(m.get("password")), "has_cv": (DATA / "CV.pdf").exists()}


def smtp_connect(m: dict):
    host, port = PRESETS.get(m.get("preset", ""), (m.get("smtp_host", ""), int(m.get("smtp_port") or 587)))
    if not host:
        raise ValueError("No SMTP server set")
    s = smtplib.SMTP(host, port, timeout=30)
    s.ehlo(); s.starttls(context=ssl.create_default_context()); s.ehlo()
    s.login(m["user"], m["password"])
    return s


def tracker_rows() -> list[dict]:
    t = DATA / "tracker.csv"
    return list(csv.DictReader(io.StringIO(t.read_text(encoding="utf-8-sig")))) if t.exists() else []


def tracker_write(rows: list[dict]):
    if not rows:
        return
    fields = list(rows[0].keys())
    with (DATA / "tracker.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)


def draft_email(row: dict) -> dict:
    """Subject + body for one tracker row, from its application pack when there is one."""
    profile = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
    c = profile["candidate"]
    body = ""
    if row.get("pack_file") and (DATA / row["pack_file"]).exists():
        md = (DATA / row["pack_file"]).read_text(encoding="utf-8")
        import re
        m = re.search(r"## Cover letter\n+([\s\S]*?)(?=\n## |$)", md)
        if m:
            body = m.group(1).strip()
            body = re.sub(r"^\d{1,2} \w+ \d{4}\n+", "", body)   # the letter's date line is odd in an e-mail
    if not body:
        body = (f"Dear Hiring Manager,\n\nI would like to apply for the {row['title']} position"
                f"{' with ' + row['company'] if row.get('company') and row['company'] not in ('Unknown', 'Gulf Job Paper') else ''}. "
                f"I am a civil engineer with {c['years_experience']} years of site and contract experience on water-network, roads and "
                f"airport infrastructure in Bahrain, NEBOSH IGC certified, available on {c['notice_period']}' notice and ready to relocate.\n\n"
                f"My CV is attached. I would welcome the chance to discuss the role.\n\nYours sincerely,\n{c['name']}\n{c['phone']} | {c['email']}")
    subject = f"Application for {row['title']} - {c['name']}"
    return {"to": row.get("apply_email", ""), "subject": subject, "body": body, "cv": "CV.pdf" if (DATA / "CV.pdf").exists() else ""}


def send_application(url: str, to: str, subject: str, body: str) -> dict:
    m = mail_settings()
    if not (m.get("user") and m.get("password")):
        raise ValueError("E-mail settings are not complete (address + app password)")
    cv = DATA / "CV.pdf"
    if not cv.exists():
        raise ValueError("No CV attached yet - choose your CV PDF in the e-mail settings")
    msg = EmailMessage()
    msg["From"] = formataddr((m.get("from_name") or m["user"], m["user"]))
    msg["To"] = ", ".join(x.strip() for x in to.split(",") if x.strip())
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
    (sent / f"{stamp}_{safe}.eml").write_bytes(bytes(msg))
    rows = tracker_rows()
    for r in rows:
        if r.get("url") == url:
            r["status"] = "Applied"; r["applied_date"] = r.get("applied_date") or datetime.now().date().isoformat()
            r["user_notes"] = (r.get("user_notes", "") + f" | e-mailed {msg['To']} {stamp[:10]}").strip(" |")
    tracker_write(rows)
    return {"ok": True, "to": msg["To"], "saved": f"sent/{stamp}_{safe}.eml"}


# ----------------------------------------------------------------------------- apply on site (browser helper)
APPLY: dict[str, object] = {}


def apply_on_site(url: str) -> dict:
    from agent import browser
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
    sess = browser.ApplySession(url, profile, job, cv if cv.exists() else None, f"http://127.0.0.1:{PORT}", profile_dir=DATA / "browser")
    APPLY[url] = sess
    return {"ok": True, "message": "opened in the apply window", "cv": cv.exists()}


def mark_status(url: str, status: str) -> dict:
    rows = tracker_rows()
    hit = False
    for r in rows:
        if r.get("url") == url:
            hit = True
            r["status"] = status
            if status == "Applied":
                r["applied_date"] = r.get("applied_date") or datetime.now().date().isoformat()
    if hit:
        tracker_write(rows)
    return {"ok": hit}


# ----------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code=200, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if urlparse(self.path).path == PUBLIC_PATH:      # the only call job sites may make (the apply bar's "I submitted")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _safe(self, rel: str) -> Path | None:
        """Files the page may read/write: text files in the data folder, never the mail password, CV, sent mail or browser profile."""
        p = (DATA / rel).resolve()
        root = DATA.resolve()
        if not p.is_relative_to(root) or p == root:
            return None
        parts = p.relative_to(root).parts
        if parts[0] in PRIVATE_DIRS or p.name in PRIVATE_FILES or p.suffix.lower() not in (".json", ".csv", ".md", ".txt", ".log"):
            return None
        return p

    def _trusted(self) -> bool:
        """Only this app's own page may use the API. Blocks other websites (Origin) and DNS-rebinding (Host)."""
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return urlparse(self.path).path == PUBLIC_PATH and self.command in ("POST", "OPTIONS")
        return True

    def do_OPTIONS(self):
        if urlparse(self.path).path == PUBLIC_PATH and self._trusted():
            return self._send(204, b"")
        return self._send(403, b"")

    def do_GET(self):
        if not self._trusted():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, (BUNDLE / "panel_local.html").read_bytes(), "text/html; charset=utf-8")
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
            return self._json({u_: {"stopped": s_.stop, "log": s_.log[-5:]} for u_, s_ in APPLY.items()})
        if u.path == "/api/draft":
            url = q.get("url", [""])[0]
            row = next((r for r in tracker_rows() if r.get("url") == url), None)
            if not row:
                return self._json({"error": "job not in tracker"}, 404)
            return self._json(draft_email(row))
        return self._json({"error": "no route"}, 404)

    def do_POST(self):
        if not self._trusted():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path); q = parse_qs(u.query)
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n) if n else b""
        if u.path == "/api/file":
            p = self._safe(q.get("path", [""])[0])
            if not p:
                return self._json({"error": "bad path"}, 400)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.loads(body).get("text", ""), encoding="utf-8")
            return self._json({"ok": True})
        if u.path == "/api/run":
            run_agent(); return self._json({"ok": True})
        if u.path == "/api/schedule":
            d = json.loads(body or b"{}")
            return self._json({"ok": True, "message": schedule(bool(d.get("enable")), int(d.get("hour", 7))), "scheduled": schedule_status()})
        if u.path == "/api/mail":
            d = json.loads(body or b"{}")
            m = mail_settings()
            for k in ("preset", "smtp_host", "smtp_port", "user", "from_name", "cc_self"):
                if k in d:
                    m[k] = d[k]
            if d.get("password"):
                m["password"] = d["password"]
            (DATA / MAIL_FILE).write_text(json.dumps(m, indent=2), encoding="utf-8")
            return self._json({"ok": True} | mail_public())
        if u.path == "/api/mail-test":
            try:
                with smtp_connect(mail_settings()):
                    pass
                return self._json({"ok": True, "message": "Signed in to the mail server - sending will work."})
            except Exception as e:
                return self._json({"ok": False, "message": f"{type(e).__name__}: {e}"})
        if u.path == "/api/cv":
            d = json.loads(body or b"{}")
            raw = base64.b64decode(d.get("data", ""))
            if not raw.startswith(b"%PDF"):
                return self._json({"ok": False, "error": "Please choose a PDF file"})
            (DATA / "CV.pdf").write_bytes(raw)
            m = mail_settings(); m["cv_name"] = d.get("name") or "CV.pdf"
            (DATA / MAIL_FILE).write_text(json.dumps(m, indent=2), encoding="utf-8")
            return self._json({"ok": True, "name": m["cv_name"]})
        if u.path == "/api/send":
            d = json.loads(body or b"{}")
            try:
                return self._json(send_application(d["url"], d["to"], d["subject"], d["body"]))
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/apply-site":
            d = json.loads(body or b"{}")
            try:
                return self._json(apply_on_site(d["url"]))
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        if u.path == PUBLIC_PATH:
            d = json.loads(body or b"{}")
            if d.get("url") not in APPLY:            # only jobs you opened with "Apply on site", only to Applied
                return self._json({"ok": False, "error": "unknown job"}, 403)
            return self._json(mark_status(d["url"], "Applied"))
        if u.path == "/api/open-folder":
            try:
                if os.name == "nt": os.startfile(DATA)  # type: ignore[attr-defined]
                else: subprocess.Popen(["xdg-open", str(DATA)])
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
            return self._json({"ok": True})
        return self._json({"error": "no route"}, 404)


def serve(open_browser=True):
    ensure_data()
    sys.path.insert(0, str(BUNDLE))
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
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
