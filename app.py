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
PRIVATE_FILES = {"mail.json", "cv.pdf", "keys.json"}          # never readable through /api/file (names compared in lower case)
PRIVATE_DIRS = {"browser", "browser-search", "sent"}
# /api/file is an allow-list: the only files the page reads, and the only ones it may write.
PAGE_READS = {"tracker.csv", "profile.json", "sources.json", "runs.log"}
PAGE_WRITES = {"profile.json", "sources.json"}
JOOBLE_SITES = ("us", "sa", "ae", "qa", "kw", "om", "bh", "ca", "au")


def ensure_data():
    from agent import tracker
    DATA.mkdir(exist_ok=True)
    (DATA / "applications").mkdir(exist_ok=True)
    for name in ("profile.json", "keys.json", "sources.json"):
        if not (DATA / name).exists():
            shutil.copy(BUNDLE / name, DATA / name)
    new_rules = upgrade_config()
    if not (DATA / "tracker.csv").exists():
        tracker.write(DATA / "tracker.csv", {})
    else:
        try:                                         # one-time re-tag of rows from older versions (display tags only)
            with tracker.locked(DATA / "tracker.csv"):
                rows = tracker.load_current(DATA / "tracker.csv")     # also adds results kept aside while Excel had it open
                profile = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
                changed = tracker.retag_old_rows(rows, profile)
                if new_rules:
                    changed = rejudge_after_rules_update(rows, profile) or changed
                if changed:
                    tracker.write(DATA / "tracker.csv", rows)
        except Exception as e:  # noqa
            print(f"job list update skipped: {e}")


def rejudge_after_rules_update(rows: dict, profile: dict) -> int:
    """A new program version with better matching rules: the jobs the agent judged (not the ones you acted on) are judged
    again on their saved ads right away, so the lists are right the moment the new version opens - not after a search."""
    from agent import tracker
    from agent.util import countries
    det = _details()
    tracker.backfill_from_notes(rows)
    n = tracker.rescore_with_new_rules(rows, profile, det.load, countries(profile, "apply"))
    n += tracker.reopen_after_rule_changes(rows, profile, det.load)
    n += tracker.release_found(rows, countries(profile, "apply"))
    n += len(tracker.merge_duplicates(rows))
    n += len(tracker.sweep_titles(rows, profile))
    if n:
        print(f"New matching rules: {n} jobs in your list were judged again")
    return n


# Rule sets the program owns: replaced when a newer program version ships new rules (_rules_version goes up).
# Your own facts and choices (candidate, form answers, evidence, countries, queries, minimum scores) are never changed.
RULE_KEYS = ("hard_reject_patterns", "title_reject_patterns", "title_penalties", "title_penalty_exempt", "sectors", "work_rights",
             "screening_answers", "target_titles", "keyword_points", "levels", "generic_titles")
CATEGORY_RULE_KEYS = ("target_titles", "keyword_points", "requires_sector", "require_any", "label")


def _add_missing(dst: dict, src: dict) -> bool:
    changed = False
    for k, v in src.items():
        if k not in dst:
            dst[k] = v; changed = True
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            changed = _add_missing(dst[k], v) or changed
    return changed


def upgrade_config() -> bool:
    """Bring the data folder's settings up to this program version - once per version, never undoing your own edits.
    True when the matching rules were updated."""
    from agent import tracker
    upgraded = False
    try:
        user = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
        bundled = json.loads((BUNDLE / "profile.json").read_text(encoding="utf-8"))
        if int(user.get("_rules_version", 0)) < int(bundled.get("_rules_version", 0)):
            mine_visa = {c: v.get("visa_needed") for c, v in (user.get("work_rights") or {}).items()
                         if isinstance(v, dict) and "visa_needed" in v}
            for k in RULE_KEYS:
                if k in bundled:
                    user[k] = bundled[k]
            for c, v in mine_visa.items():                   # your own "I don't need a visa there" survives rule updates
                if isinstance(user.get("work_rights", {}).get(c), dict):
                    user["work_rights"][c]["visa_needed"] = v
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
            upgraded = True
    except Exception as e:  # noqa
        print(f"profile upgrade skipped: {e}")
    try:
        user = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
        bundled = json.loads((BUNDLE / "sources.json").read_text(encoding="utf-8"))
        if int(user.get("_version", 0)) < int(bundled.get("_version", 0)):
            # sources are program-owned, except the on/off choices you can make in the app
            mine, new = user.get("browser_sites") or {}, bundled.get("browser_sites")
            if isinstance(mine, dict) and isinstance(new, dict):
                for k in ("show_window", "enabled"):
                    if k in mine:
                        new[k] = mine[k]
                if isinstance(mine.get("sites"), dict):
                    new.setdefault("sites", {}).update({k: v for k, v in mine["sites"].items() if k in new.get("sites", {})})
            tracker.atomic_write(DATA / "sources.json", json.dumps(bundled, indent=2, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa
        print(f"sources upgrade skipped: {e}")
    return upgraded


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


def _task_xml(command: str, args: str, hour: int) -> str:
    """A daily task that also runs when the PC was off or asleep at that time (as soon as it is available), on battery
    too, and is stopped if a search ever runs longer than 2 hours."""
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Job Agent daily job search</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>2026-01-01T{hour:02d}:00:00</StartBoundary><Enabled>true</Enabled>
    <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable><ExecutionTimeLimit>PT2H</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>{escape(command)}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions>
</Task>"""


_SCHED = {"at": 0.0, "on": False}


def schedule(enable: bool, hour: int = 7) -> str:
    if os.name != "nt":
        return "Scheduling is available on Windows only."
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _SCHED["at"] = 0.0
    if not enable:
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK], capture_output=True, text=True, creationflags=flags)
        return (r.stdout or r.stderr).strip()
    command, args = (sys.executable, "--run") if FROZEN else (sys.executable, f'"{Path(__file__).resolve()}" --run')
    xml = DATA / "schedule-task.xml"
    try:
        xml.write_text(_task_xml(command, args, hour), encoding="utf-16")
        r = subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK, "/XML", str(xml)], capture_output=True, text=True, creationflags=flags)
    finally:
        xml.unlink(missing_ok=True)
    if r.returncode != 0:                       # older Windows: the plain daily task (runs only if the PC is on at that time)
        tr = f'"{command}" {args}'
        r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", TASK, "/ST", f"{hour:02d}:00", "/TR", tr],
                           capture_output=True, text=True, creationflags=flags)
    if r.returncode == 0:
        try:
            (DATA / "schedule.json").write_text(json.dumps({"hour": hour}), encoding="utf-8")
        except OSError:
            pass
    return (r.stdout or r.stderr).strip()


def schedule_status() -> bool:
    """Is the daily task set up? (Checked at most once a minute.)"""
    if os.name != "nt":
        return False
    if time.time() - _SCHED["at"] > 60:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _SCHED.update(at=time.time(), on=r.returncode == 0)
    return _SCHED["on"]


def schedule_hour() -> int:
    try:
        return int(json.loads((DATA / "schedule.json").read_text(encoding="utf-8")).get("hour", 7))
    except Exception:
        return 7


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


def tracker_rows_current() -> list[dict]:
    """The list including results waiting in tracker.pending.csv (caller holds the tracker lock)."""
    from agent import tracker
    return list(tracker.load_current(DATA / "tracker.csv").values())


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
    with tracker.locked(DATA / "tracker.csv"):
        rows = tracker_rows_current()
        for r in rows:
            if r.get("url") == url:
                r["status"] = "Applied"; r["applied_date"] = r.get("applied_date") or datetime.now().date().isoformat()
                r["touched"] = datetime.now().date().isoformat()
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
    with tracker.locked(DATA / "tracker.csv"):            # also blocks the scheduled search (separate process) meanwhile
        rows = tracker_rows_current()
        hit = False
        for r in rows:
            if r.get("url") == url:
                hit = True
                r["status"] = status
                r["touched"] = datetime.now().date().isoformat()
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
    with tracker.locked(DATA / "tracker.csv"):            # also blocks the scheduled search (separate process) meanwhile
        rows = tracker_rows_current()
        n = 0
        today = datetime.now().date().isoformat()
        for r in rows:
            e = edits.get(r.get("url"))
            if not isinstance(e, dict):
                continue
            for k, v in e.items():
                if k in EDITABLE and isinstance(v, str) and len(v) <= 2000 and (k != "status" or v in STATUSES):
                    if r.get(k, "") != v:
                        r[k] = v
                        r["touched"] = today
                        n += 1
        if n:
            tracker_write(rows, locked=True)
        missing = set(edits) - {r.get("url") for r in rows}
    if missing and not n:              # e.g. a search merged the job with the same ad from another site meanwhile
        return {"ok": False, "changed": 0, "error": "this job is no longer in the list - press F5 to reload"}
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
    _nonce = ""

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
            self.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{self._nonce}'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _safe(self, rel: str, write: bool = False) -> Path | None:
        """The page may read the job list, settings and search history, and write only the two settings files - by exact
        name, in the data folder itself. Everything else (mail password, keys, CV, sent mail, browser profiles) is off limits."""
        name = (rel or "").strip()
        if name not in (PAGE_WRITES if write else PAGE_READS):
            return None
        p = DATA / name
        if p.is_symlink():
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
            self._nonce = secrets.token_urlsafe(18)    # only the app's own script may run on the page, even if a bug let markup in
            html = (BUNDLE / "panel_local.html").read_text(encoding="utf-8").replace("__JOBAGENT_TOKEN__", TOKEN)
            html = html.replace("<script>", f'<script nonce="{self._nonce}">', 1)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/file":
            p = self._safe(q.get("path", [""])[0])
            if not p or not p.exists():
                return self._json({"error": "not found"}, 404)
            if p.name == "tracker.csv":
                # the list as the app reads it - also after Excel re-saved it (other encoding, ";" separator, dd/mm dates),
                # with results still waiting while Excel has it open
                from agent import tracker
                try:
                    with tracker.locked(p):
                        rows = tracker.load_current(p)
                except tracker.Unreadable as e:
                    return self._json({"error": str(e)}, 500)
                return self._json({"text": tracker.to_csv(rows)})
            return self._json({"text": p.read_text(encoding="utf-8", errors="replace")})
        if u.path == "/api/status":
            runs = []
            if (DATA / "runs.log").exists():
                runs = [l.split("\t", 1) for l in (DATA / "runs.log").read_text(encoding="utf-8").splitlines()[-8:] if "\t" in l]
            lock = DATA / RUN_LOCK
            elsewhere = not STATE["running"] and lock.exists() and time.time() - lock.stat().st_mtime < 3 * 3600
            return self._json({"running": STATE["running"] or elsewhere, "elsewhere": elsewhere,
                               "log": STATE["log"][-6:] if not elsewhere else ["The daily search is running in the background"],
                               "summary": STATE["summary"], "started": STATE["started"],
                               "runs": [{"at": a, "summary": b} for a, b in reversed(runs)], "scheduled": schedule_status(),
                               "schedule_hour": schedule_hour(), "data_dir": str(DATA)})
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
        if u.path == "/api/keys":            # which keys are saved - never the keys themselves
            jo = _keys().get("jooble") or {}
            return self._json({"jooble": {cc: bool(str(jo.get(cc) or "").strip()) for cc in JOOBLE_SITES}})
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
            p = self._safe(q.get("path", [""])[0], write=True)
            if not p:
                return self._json({"error": "this file can't be changed from the page (the job list changes through /api/edits)"}, 400)
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
            try:
                hour = int(d.get("hour", 7))
            except (TypeError, ValueError):
                hour = -1
            if not 0 <= hour <= 23:
                return self._json({"error": "hour must be 0-23"}, 400)
            return self._json({"ok": True, "message": schedule(bool(d.get("enable")), hour), "scheduled": schedule_status()})
        if u.path == "/api/mail":
            m = mail_settings()
            before = (m.get("preset"), m.get("smtp_host"), m.get("smtp_port"), m.get("user"))
            for k in ("preset", "smtp_host", "user", "from_name"):
                if k in d:
                    m[k] = str(d[k])[:200].strip()
            if "smtp_port" in d:
                try:
                    m["smtp_port"] = int(d["smtp_port"] or 587)
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "the port must be a number"}, 400)
                if not 1 <= m["smtp_port"] <= 65535:
                    return self._json({"ok": False, "error": "the port must be 1-65535"}, 400)
            if m.get("user") and not EMAIL_OK.fullmatch(m["user"]):
                return self._json({"ok": False, "error": "your e-mail address doesn't look right"}, 400)
            if m.get("smtp_host") and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", m["smtp_host"]):
                return self._json({"ok": False, "error": "the mail server name doesn't look right"}, 400)
            if before != (m.get("preset"), m.get("smtp_host"), m.get("smtp_port"), m.get("user")) and not d.get("password"):
                m.pop("password", None)                # a saved password only ever goes to the server it was saved for
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
        if u.path == "/api/keys":            # Jooble keys only; an empty value removes one
            k = _keys()
            jo = k.setdefault("jooble", {})
            for cc, v in (d.get("jooble") or {}).items():
                v = str(v or "").strip()
                if cc not in JOOBLE_SITES or len(v) > 100 or (v and not re.fullmatch(r"[A-Za-z0-9-]+", v)):
                    return self._json({"ok": False, "error": f"that doesn't look like a Jooble key ({cc})"}, 400)
                jo[cc] = v
            tracker.atomic_write(DATA / "keys.json", json.dumps(k, indent=2))
            return self._json({"ok": True})
        if u.path == "/api/ai":
            from agent import ai
            k = _keys()
            a = k.setdefault("ai", {})
            if "enabled" in d:
                a["enabled"] = bool(d["enabled"])
            if "provider" in d:
                if d["provider"] not in ai.PROVIDERS:
                    return self._json({"ok": False, "error": "unknown provider"}, 400)
                if d["provider"] != a.get("provider"):
                    a.pop("base_url", None)            # each provider uses its own fixed address
                a["provider"] = d["provider"]
            if "model" in d:
                m = str(d["model"] or "").strip()
                if len(m) > 100 or (m and not re.fullmatch(r"[\w.:/@+-]+", m)):
                    return self._json({"ok": False, "error": "that model name doesn't look right"}, 400)
                a["model"] = m
            if "max_per_run" in d:
                try:
                    a["max_per_run"] = max(1, min(900, int(d["max_per_run"])))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "ads to read per search must be a number"}, 400)
            if d.get("api_key"):
                key = str(d["api_key"]).strip()
                if len(key) > 200 or not re.fullmatch(r"[\x21-\x7e]+", key):
                    return self._json({"ok": False, "error": "that doesn't look like an API key"}, 400)
                a["api_key"] = key
            tracker.atomic_write(DATA / "keys.json", json.dumps(k, indent=2))
            return self._json({"ok": True, "active": bool(ai.settings(k)),
                               "why_off": "" if ai.settings(k) or not a.get("enabled") else
                               ("paste your Groq key to switch it on" if a.get("provider", "groq") == "groq" else "choose a provider")})
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


class AppServer(ThreadingHTTPServer):
    """At most 32 requests at a time: another program on this PC can't tie up the app by opening connections."""
    daemon_threads = True

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(open_browser=True):
    ensure_data()
    sys.path.insert(0, str(BUNDLE))
    import atexit
    atexit.register(_shutdown)
    srv = AppServer(("127.0.0.1", PORT), Handler)
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
