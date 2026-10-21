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
PORT = int(os.environ.get("JOBAGENT_PORT") or 8765)      # another port and data folder only for testing a copy
TOKEN = secrets.token_urlsafe(24)       # per-launch secret written into the page; every /api call must carry it


# ------------------------------------------------------------------ where your data lives
# The data folder used to be only "data next to the exe": starting a new version from another folder (Downloads, the
# desktop, straight from the browser) opened an empty list. Now the app remembers the folder, finds your list wherever the
# exe is started from, and keeps dated backups outside the data folder that are put back if the list ever goes missing.
APPHOME = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home()) / "JobAgent"
POINTER = APPHOME / "data-folder.txt"
BACKUPS = APPHOME / "backups"
BACKUP_FILES = ("tracker.csv", "profile.json", "keys.json", "mail.json", "sources.json", "state.json", "CV.pdf", "ai_usage.json")


def _rows_in(folder: Path) -> int:
    """Rough number of jobs in a data folder (-1 when it isn't one)."""
    if not (folder / "profile.json").exists():
        return -1
    try:
        with open(folder / "tracker.csv", "rb") as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


def data_candidates(home: Path) -> list[Path]:
    up = Path(os.environ.get("USERPROFILE") or Path.home())
    places = [up / "Documents", up / "Desktop", up / "Downloads", up / "OneDrive" / "Documents", up / "OneDrive" / "Desktop",
              up / "Documents" / "JobAgent", up / "Desktop" / "JobAgent"]
    return [home / "data"] + [p / "data" for p in places]


def locate_data(home: Path, pointer: Path = None, candidates: list[Path] = None) -> Path:
    """The folder holding your list: the one used last time if it still has jobs, else the one next to the exe if it has
    jobs, else the fullest data folder in the usual places, else a new one next to the exe."""
    pointer = pointer or POINTER
    candidates = candidates if candidates is not None else data_candidates(home)
    here = home / "data"
    try:
        last = Path(pointer.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        last = None
    if last and _rows_in(last) > 0:
        chosen = last
    elif _rows_in(here) > 0:
        chosen = here
    else:
        full = sorted(((_rows_in(c), c) for c in candidates if c != here), key=lambda x: -x[0])
        chosen = full[0][1] if full and full[0][0] > 0 else here
    try:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(str(chosen.resolve()), encoding="utf-8")
    except OSError:
        pass
    return chosen


def backup_data(data: Path, backups: Path = None, keep: int = 20, every_hours: float = 6) -> Path | None:
    """A dated copy of your list and settings, outside the data folder (at most every few hours; the last 20 are kept)."""
    backups = backups or BACKUPS
    if _rows_in(data) <= 0:
        return None
    try:
        backups.mkdir(parents=True, exist_ok=True)
        old = sorted(d for d in backups.iterdir() if d.is_dir())
        if old and time.time() - old[-1].stat().st_mtime < every_hours * 3600:
            return None
        dest = backups / datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest.mkdir()
        for name in BACKUP_FILES:
            if (data / name).exists():
                shutil.copy2(data / name, dest / name)
        for d in old[:-(keep - 1)] if len(old) >= keep else []:
            shutil.rmtree(d, ignore_errors=True)
        return dest
    except OSError:
        return None


def restore_if_missing(data: Path, backups: Path = None) -> Path | None:
    """The data folder has no jobs but a backup does (the folder was deleted or emptied): put the newest backup back."""
    backups = backups or BACKUPS
    if _rows_in(data) > 0 or not backups.exists():
        return None
    for b in sorted((d for d in backups.iterdir() if d.is_dir()), reverse=True):
        if _rows_in(b) > 0:
            data.mkdir(parents=True, exist_ok=True)
            for name in BACKUP_FILES:
                if (b / name).exists():
                    shutil.copy2(b / name, data / name)
            return b
    return None


def _build() -> str:
    """Which build this is (written by the build workflow), so a newer copy can tell an older one is still running."""
    try:
        return (BUNDLE / "build.txt").read_text(encoding="utf-8").strip()[:40] or "dev"
    except OSError:
        return "dev"


BUILD = _build()
PRIVATE_FILES = {"mail.json", "cv.pdf", "keys.json"}          # never readable through /api/file (names compared in lower case)
PRIVATE_DIRS = {"browser", "browser-search", "sent"}
# /api/file is an allow-list: the only files the page reads, and the only ones it may write.
PAGE_READS = {"tracker.csv", "profile.json", "sources.json", "runs.log"}
PAGE_WRITES = {"profile.json", "sources.json"}
JOOBLE_SITES = ("us", "sa", "ae", "qa", "kw", "om", "bh", "ca", "au")


INFO = {"restored": ""}             # set at start when the list was put back from a backup

# ------------------------------------------------------------------ updates (the app replaces itself - no downloading by hand)
REPO = "shuaibhusain99-netizen/job-agent-core"


def _gh(url: str, limit: int = 2_000_000, binary: bool = False) -> bytes:
    from urllib.request import Request, urlopen
    h = {"User-Agent": "JobAgent-updater", "Accept": "application/octet-stream" if binary else "application/vnd.github+json"}
    with urlopen(Request(url, headers=h), timeout=120 if binary else 30) as r:
        return r.read(limit)


def latest_release() -> dict:
    rel = json.loads(_gh(f"https://api.github.com/repos/{REPO}/releases/tags/latest"))
    assets = {a.get("name"): a for a in rel.get("assets") or []}
    exe, marker = assets.get("JobAgent.exe"), assets.get("build.txt")
    if not exe:
        raise RuntimeError("the latest release has no JobAgent.exe")
    build = _gh(marker["browser_download_url"], 200).decode("ascii", "replace").strip()[:40] if marker else ""
    return {"build": build, "url": exe["browser_download_url"], "size": int(exe.get("size") or 0),
            "digest": str(exe.get("digest") or ""), "date": str(exe.get("updated_at") or "")[:10]}


def _newer(latest: str) -> bool:
    return bool(latest) and BUILD != "dev" and latest.split()[0] != BUILD.split()[0]


def update_check() -> dict:
    r = latest_release()
    return {"ok": True, "available": _newer(r["build"]), "current": BUILD, "latest": r["build"], "date": r["date"]}


def install_update() -> dict:
    """Download the newest JobAgent.exe, check it, put it in place of this one (same folder, same data) and restart."""
    import hashlib
    if not FROZEN:
        raise ValueError("This is a development copy - updates are for the installed JobAgent.exe")
    lock = DATA / RUN_LOCK
    if STATE["running"] or (lock.exists() and time.time() - lock.stat().st_mtime < 3 * 3600):
        raise ValueError("A search is running - update when it has finished")
    r = latest_release()
    if not _newer(r["build"]):
        raise ValueError("You already have the newest version")
    blob = _gh(r["url"], 300_000_000, binary=True)
    if len(blob) < 3_000_000 or blob[:2] != b"MZ" or (r["size"] and len(blob) != r["size"]):
        raise RuntimeError("the download was incomplete - try again")
    if r["digest"].startswith("sha256:") and hashlib.sha256(blob).hexdigest() != r["digest"][7:].lower():
        raise RuntimeError("the download didn't match the published program - nothing was changed")
    exe = Path(sys.executable)
    new = exe.with_name(exe.stem + ".new.exe")
    new.write_bytes(blob)
    old = exe.with_name(exe.stem + ".old.exe")
    try:
        old.unlink(missing_ok=True)
    except OSError:                                    # still in use from the last update: use another name
        old = exe.with_name(f"{exe.stem}.old-{int(time.time())}.exe")
    exe.rename(old)                                    # Windows lets a running program be renamed (not deleted)
    try:
        new.rename(exe)
    except OSError:
        old.rename(exe)
        raise
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe), "--no-browser"], cwd=str(exe.parent), creationflags=flags, close_fds=True)
    threading.Thread(target=quit_app, args=(1.5,), daemon=True).start()   # the new copy takes over the port
    return {"ok": True, "message": f"Updating to the version of {r['date']} - the page reloads in a few seconds"}


def tidy_after_update() -> None:  # runs at every start
    """Remove the previous program file left by an update (it can only be deleted once it has stopped)."""
    if not FROZEN:
        return
    exe = Path(sys.executable)
    for _ in range(6):
        left = [f for f in exe.parent.glob(exe.stem + ".old*.exe")] + [f for f in exe.parent.glob(exe.stem + ".new.exe")]
        for f in left:
            try:
                f.unlink()
            except OSError:
                pass
        if not left:
            return
        time.sleep(10)


UPDATING = {"on": False}            # the list is being re-judged after a program update (the page says so)


def ensure_data(background: bool = False):
    """Data folder ready. The slow part after a program update (re-judging the whole list) runs in the background when
    `background` is set, so the app's page opens at once."""
    from agent import tracker, ai
    DATA.mkdir(exist_ok=True)
    ai.USAGE = DATA / "ai_usage.json"
    (DATA / "applications").mkdir(exist_ok=True)
    for name in ("profile.json", "keys.json", "sources.json"):
        if not (DATA / name).exists():
            shutil.copy(BUNDLE / name, DATA / name)
    new_rules = upgrade_config()
    if not (DATA / "tracker.csv").exists():
        tracker.write(DATA / "tracker.csv", {})
        return

    def update():
        try:                                         # one-time re-tag of rows from older versions (display tags only)
            with tracker.locked(DATA / "tracker.csv", wait=300):
                rows = tracker.load_current(DATA / "tracker.csv")     # also adds results kept aside while Excel had it open
                profile = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
                changed = tracker.retag_old_rows(rows, profile)
                if new_rules:
                    changed = rejudge_after_rules_update(rows, profile) or changed
                if changed:
                    tracker.write(DATA / "tracker.csv", rows)
        except Exception as e:  # noqa
            print(f"job list update skipped: {e}")
        finally:
            UPDATING["on"] = False
    if new_rules and background:
        UPDATING["on"] = True
        threading.Thread(target=update, daemon=True).start()
    else:
        update()


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


def alert(text: str, title: str = "Job Agent") -> None:
    """A message the person can see even though the packaged app has no console (a Windows message box)."""
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)      # MB_ICONERROR
            return
    except Exception:
        pass
    print(f"{title}: {text}", file=sys.stderr if sys.stderr else sys.stdout)


# ----------------------------------------------------------------------------- agent run
STATE = {"running": False, "log": [], "started": "", "finished": "", "summary": ""}
RUN_LOCK = "search.lock"
LAST_POLL = [time.time()]            # when the page last asked for the status: the app quits itself after a long silence
IDLE_QUIT = 2 * 3600


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


def _run_quiet(args: list[str], timeout: int = 30):
    """Run a Windows tool without a console window; its output is read in whatever language/code page Windows uses."""
    r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    def dec(b: bytes) -> str:
        for enc in ("utf-8", "mbcs" if os.name == "nt" else "latin-1", "cp850"):
            try:
                return b.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return b.decode("latin-1", "replace")
    r.stdout, r.stderr = dec(r.stdout or b""), dec(r.stderr or b"")
    return r


def schedule(enable: bool, hour: int = 7) -> str:
    if os.name != "nt":
        return "Scheduling is available on Windows only."
    _SCHED["at"] = 0.0
    if not enable:
        r = _run_quiet(["schtasks", "/Delete", "/F", "/TN", TASK])
        return (r.stdout or r.stderr).strip()
    command, args = (sys.executable, "--run") if FROZEN else (sys.executable, f'"{Path(__file__).resolve()}" --run')
    xml = DATA / "schedule-task.xml"
    try:
        xml.write_text(_task_xml(command, args, hour), encoding="utf-16")
        r = _run_quiet(["schtasks", "/Create", "/F", "/TN", TASK, "/XML", str(xml)])
    finally:
        xml.unlink(missing_ok=True)
    if r.returncode != 0:                       # older Windows: the plain daily task (runs only if the PC is on at that time)
        tr = f'"{command}" {args}'
        r = _run_quiet(["schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", TASK, "/ST", f"{hour:02d}:00", "/TR", tr])
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
        try:
            r = _run_quiet(["schtasks", "/Query", "/TN", TASK])
            on = r.returncode == 0
        except Exception:  # noqa
            on = _SCHED["on"]
        _SCHED.update(at=time.time(), on=on)
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


MS_PERSONAL = re.compile(r"@(outlook|hotmail|live|msn)\.[a-z.]+$", re.I)
GMAIL_FIX = ("Use a Gmail address instead: turn on 2-Step Verification in your Google account, create an app password at "
             "myaccount.google.com/apppasswords, then put that address and the 16-letter app password in Settings > E-mail.")


def app_password_problem(preset: str, pw: str) -> str:
    """Gmail and Yahoo only take a 16-letter app password from apps - a normal password is refused every time."""
    if preset in ("gmail", "yahoo") and pw and not re.fullmatch(r"[A-Za-z]{16}", re.sub(r"\s", "", pw)):
        name = "Gmail" if preset == "gmail" else "Yahoo"
        n = len(re.sub(r"\s", "", pw))
        where = "myaccount.google.com/apppasswords (2-Step Verification must be on)" if preset == "gmail" else "Yahoo Account Security > Generate app password"
        return (f"That isn't a {name} app password - those are exactly 16 letters (like abcd efgh ijkl mnop), and this one is "
                f"{n} characters. It looks like your normal password, which {name} refuses from apps. "
                f"Create an app password at {where} and paste it in.")
    return ""


def mail_blocked(m: dict) -> str:
    """Why these settings can't work at all (Outlook.com / Hotmail stopped password sign-in for apps in 2026), or ''."""
    bad_pw = app_password_problem(m.get("preset", ""), m.get("password", ""))
    if bad_pw:
        return bad_pw
    host = PRESETS.get(m.get("preset", ""), (m.get("smtp_host", ""), 0))[0].lower()
    if "outlook.com" in host or (MS_PERSONAL.search(m.get("user", "")) and "office365" not in host and m.get("preset") != "gmail"):
        return ("Outlook.com / Hotmail no longer lets apps sign in with a password - Microsoft switched it off in 2026. " + GMAIL_FIX)
    return ""


def mail_public() -> dict:
    m = mail_settings()
    return ({k: v for k, v in m.items() if k not in ("password", "password_dpapi")}
            | {"has_password": bool(m.get("password")), "has_cv": (DATA / "CV.pdf").exists(), "blocked": mail_blocked(m)})


def note_signin(ok: bool, reason: str = ""):
    """Remember whether the last sign-in worked, so the page can warn before you write an e-mail that can't be sent."""
    try:
        m = mail_settings()
        if m:
            m["signin"], m["signin_error"] = ("ok", "") if ok else ("failed", reason[:600])
            save_mail(m)
    except Exception:  # noqa - only a hint
        pass


def smtp_connect(m: dict):
    host, port = PRESETS.get(m.get("preset", ""), (m.get("smtp_host", ""), int(m.get("smtp_port") or 587)))
    if not host:
        raise ValueError("No SMTP server set")
    if not m.get("user") or not m.get("password"):
        raise ValueError("E-mail address and app password are needed")
    if mail_blocked(m):
        raise smtplib.SMTPAuthenticationError(535, b"basic authentication is disabled")   # don't trigger a Microsoft security alert
    s = smtplib.SMTP(host, port, timeout=30)
    s.ehlo(); s.starttls(context=ssl.create_default_context()); s.ehlo()
    try:
        s.login(m["user"], m["password"])
    except smtplib.SMTPServerDisconnected:          # Gmail hangs up on a normal (non-app) password instead of saying no
        raise smtplib.SMTPAuthenticationError(535, b"the server closed the connection at sign-in") from None
    return s


def mail_error(e: Exception, m: dict | None = None) -> tuple[str, bool]:
    """A sign-in or sending failure in plain words, and whether fixing it means changing the e-mail settings."""
    m = m or {}
    host = PRESETS.get(m.get("preset", ""), (m.get("smtp_host", ""), 0))[0].lower()
    user = m.get("user", "")
    if isinstance(e, ValueError):
        return str(e), "settings" in str(e).lower() or "app password" in str(e).lower()
    if isinstance(e, smtplib.SMTPAuthenticationError):
        if "outlook.com" in host or (MS_PERSONAL.search(user) and "office365" not in host):
            return ("Outlook.com / Hotmail no longer lets apps sign in with a password - Microsoft switched it off in 2026 - "
                    f"so the agent can't send from {user or 'that address'}. " + GMAIL_FIX), True
        if "office365" in host:
            return ("Microsoft 365 refused the sign-in. Many companies switch off app sign-in for e-mail; ask your IT team, or "
                    + GMAIL_FIX[0].lower() + GMAIL_FIX[1:]), True
        if "gmail" in host:
            return ("Gmail refused the sign-in. It needs an app password (16 letters from myaccount.google.com/apppasswords, "
                    "with 2-Step Verification on) - your normal Gmail password won't work. Put it in Settings > E-mail."), True
        return "The mail server refused your address and app password. Check them in Settings > E-mail.", True
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the To address (" + ", ".join(e.recipients) + "). Check it and send again.", False
    if isinstance(e, smtplib.SMTPSenderRefused):
        return f"The mail server won't send from {user or 'your address'}: {e.smtp_code} {e.smtp_error.decode(errors='replace')[:160]}", True
    if isinstance(e, smtplib.SMTPResponseException):
        return f"The mail server didn't accept the e-mail: {e.smtp_code} {e.smtp_error.decode(errors='replace')[:160]}", False
    if isinstance(e, (OSError, smtplib.SMTPException)):
        return (f"Couldn't reach the mail server ({host or 'not set'}). Check your internet connection and try again"
                + (" - or check the SMTP server and port in Settings > E-mail." if not m.get("preset") else ".")), not m.get("preset")
    return f"{type(e).__name__}: {e}", False


def mail_fail(e: Exception) -> dict:
    try:
        m = mail_settings()
    except Exception:  # noqa
        m = {}
    text, fix = mail_error(e, m)
    if isinstance(e, smtplib.SMTPAuthenticationError):
        note_signin(False, text)
    return {"ok": False, "error": text + ("" if isinstance(e, ValueError) else " Nothing was sent."), "settings": fix}


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
    from agent.tailor import clean_title
    profile = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
    c = profile["candidate"]
    title = clean_title(row.get("title", ""))
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
        body = (f"Dear Hiring Manager,\n\nI would like to apply for the {title} position"
                f"{' with ' + row['company'] if row.get('company') and row['company'] not in ('Unknown', 'Gulf Job Paper') else ''}. "
                f"I am a civil engineer with {years_experience(profile)} years of site and contract experience on water-network, roads and "
                f"airport infrastructure in Bahrain, NEBOSH IGC certified, available on {c['notice_period']}' notice and ready to relocate.\n\n"
                f"My CV is attached. I would welcome the chance to discuss the role.\n\nYours sincerely,\n{c['name']}\n{c['phone']} | {c['email']}")
    subject = f"Application for {title} - {c['name']}"
    return {"to": row.get("apply_email", ""), "subject": subject, "body": body, "cv": "CV.pdf" if (DATA / "CV.pdf").exists() else ""}


def manual_job(company: str, position: str, country: str = "", ad: str = "") -> dict:
    """A job you found yourself (a company e-mail from a friend, a newspaper, a website): shaped like a searched job."""
    from agent.util import clean, guess_country
    company, position = clean(company)[:120], clean(position)[:150]
    if len(position) < 2:
        raise ValueError("Write the position you are applying for")
    if len(company) < 2:
        raise ValueError("Write the company name")
    country = country if country in COUNTRY_NAMES else (guess_country(clean(ad)[:400]) if ad else "")
    return {"title": position, "company": company, "location": country or "Not specified", "country": country if country != "Other" else "",
            "description": clean(ad or "", keep_lines=True)[:8000], "source": "Your e-mail", "url": "", "posted": ""}


COUNTRY_NAMES = ("Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Oman", "Bahrain", "Canada", "Australia")


def manual_draft(company: str, position: str, to: str, country: str = "", ad: str = "", use_ai: bool = True) -> dict:
    """Subject + letter for a company you name: written for that position (and for the ad, if you pasted it)."""
    from agent import tailor
    from agent.score import score
    profile = json.loads((DATA / "profile.json").read_text(encoding="utf-8"))
    c = profile["candidate"]
    j = manual_job(company, position, country, ad)
    probe = dict(j, description=j["description"] or j["title"])
    score(probe, profile)                                  # only to know the kind of role (civil, HSE, QS ...) for the letter
    j["category"] = probe.get("category", "civil")
    body, by_ai = "", False
    if use_ai and len(j["description"]) >= 150:
        from agent import ai
        s_ = ai.settings(_keys())
        if s_:
            try:
                body, by_ai = ai.letter(j, profile, s_), True
            except Exception:  # noqa - the standard letter below is used instead
                body = ""
    if not body:
        body = re.sub(r"^\d{1,2} \w+ \d{4}\n+", "", tailor.cover_letter(j, profile))
        body = body.replace("I would welcome the chance to discuss how my experience fits your project.",
                            "My CV is attached, and I would welcome the chance to discuss how my experience fits your team.", 1)
    return {"to": to, "subject": f"Application for {tailor.clean_title(j['title'])} - {c['name']}", "body": body,
            "by_ai": by_ai, "cv": (DATA / "CV.pdf").exists()}


def _deliver(to: str, subject: str, body: str):
    """Send one application e-mail from your own address with your CV attached; keep a copy in data/sent."""
    rcpt = [x.strip() for x in re.split(r"[,;\s]+", to) if x.strip()]
    if not rcpt or len(rcpt) > 3 or not all(EMAIL_OK.fullmatch(x) for x in rcpt):
        raise ValueError("Check the To address (up to 3 plain e-mail addresses)")
    m = mail_settings()
    if not (m.get("user") and m.get("password")):
        raise ValueError("E-mail settings are not complete (address + app password)")
    cv = DATA / "CV.pdf"
    if not cv.exists():
        raise ValueError("No CV attached yet - choose your CV PDF in the e-mail settings")
    if not subject.strip() or len(subject) > 200 or len(body) > 20000 or "\n" in subject or "\r" in subject:
        raise ValueError("Subject or message is too long (or empty)")
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
    if m.get("signin") != "ok":
        note_signin(True)
    sent = DATA / "sent"; sent.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe = "".join(ch if ch.isalnum() else "-" for ch in subject)[:60].strip("-")
    (sent / f"{stamp}_{safe}.eml").write_bytes(bytes(msg))     # your own copy; sent/ is never served to the page
    return msg, stamp, safe


def send_manual(company: str, position: str, to: str, subject: str, body: str, country: str = "", ad: str = "") -> dict:
    """E-mail a company you named, then add it to Applications so it is tracked like every other application."""
    import hashlib
    from agent import tracker
    j = manual_job(company, position, country, ad)
    msg, stamp, safe = _deliver(to, subject, body)
    today = datetime.now().date().isoformat()
    url = "manual:" + hashlib.sha1(f"{msg['To']}|{j['title']}|{j['company']}|{stamp}".encode()).hexdigest()[:16]
    row = {k: "" for k in tracker.FIELDS}
    row.update(found_date=today, last_seen=today, score="", verdict="apply", title=j["title"], company=j["company"],
               location=j["location"], country=j["country"], source="Your e-mail", url=url, status="Applied", applied_date=today,
               touched=today, notes="you sent this application from the app", user_notes=f"e-mailed {msg['To']} {stamp[:10]}",
               apply_email=msg["To"], desc_len=str(len(j["description"])), category="civil")
    warn = ""
    try:
        if j["description"]:
            _details().save(url, {"title": j["title"], "company": j["company"], "description": j["description"], "source": "Your e-mail"})
        with tracker.locked(DATA / "tracker.csv"):
            rows = tracker_rows_current()
            rows.append(row)
            tracker_write(rows, locked=True)
    except Exception as e:  # noqa - the e-mail is out whatever happens here
        warn = f"the job list couldn't be updated ({type(e).__name__}) - the e-mail itself was sent"
    return {"ok": True, "to": msg["To"], "saved": f"sent/{stamp}_{safe}.eml", "warning": warn, "url": url}


def send_application(url: str, to: str, subject: str, body: str) -> dict:
    msg, stamp, safe = _deliver(to, subject, body)
    return _record_sent(url, msg, stamp, safe)


def _record_sent(url: str, msg, stamp: str, safe: str) -> dict:
    # the e-mail is out: from here on nothing may turn this into "not sent"
    from agent import tracker
    warn = ""
    try:
        with tracker.locked(DATA / "tracker.csv"):
            rows = tracker_rows_current()
            hit = False
            for r in rows:
                if r.get("url") == url:
                    hit = True
                    r["status"] = "Applied"; r["applied_date"] = r.get("applied_date") or datetime.now().date().isoformat()
                    r["touched"] = datetime.now().date().isoformat()
                    r["user_notes"] = (r.get("user_notes", "") + f" | e-mailed {msg['To']} {stamp[:10]}").strip(" |")
            if hit:
                tracker_write(rows, locked=True)
            else:
                warn = "this job is no longer in your list, so it couldn't be marked Applied"
    except Exception as e:  # noqa - e.g. the list is open in Excel
        warn = f"the job list couldn't be updated ({type(e).__name__}) - mark it Applied yourself"
    return {"ok": True, "to": msg["To"], "saved": f"sent/{stamp}_{safe}.eml", "warning": warn}


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
            changed = False
            for k, v in e.items():
                if k in EDITABLE and isinstance(v, str) and len(v) <= 2000 and (k != "status" or v in STATUSES):
                    if r.get(k, "") != v:
                        r[k] = v
                        changed = True
                        n += 1
            t = e.get("touched")
            if isinstance(t, str) and (t == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", t)):
                if r.get("touched", "") != t:                  # Undo puts back exactly what was there - the agent may
                    r["touched"] = t                           # judge the row again as before
                    n += 1
            elif changed:
                r["touched"] = today
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
            html = html.replace("<title>", f'<meta name="jobagent-build" content="{BUILD}">\n<title>', 1)
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
            LAST_POLL[0] = time.time()
            runs = []
            if (DATA / "runs.log").exists():
                runs = [l.split("\t", 1) for l in (DATA / "runs.log").read_text(encoding="utf-8").splitlines()[-8:] if "\t" in l]
            lock = DATA / RUN_LOCK
            elsewhere = not STATE["running"] and lock.exists() and time.time() - lock.stat().st_mtime < 3 * 3600
            return self._json({"running": STATE["running"] or elsewhere, "elsewhere": elsewhere, "updating": UPDATING["on"],
                               "log": STATE["log"][-6:] if not elsewhere else ["The daily search is running in the background"],
                               "summary": STATE["summary"], "started": STATE["started"],
                               "runs": [{"at": a, "summary": b} for a, b in reversed(runs)], "scheduled": schedule_status(),
                               "schedule_hour": schedule_hour(), "data_dir": str(DATA), "build": BUILD,
                               "restored": INFO["restored"]})
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
                pw = str(d["password"])
                if m.get("preset") in ("gmail", "yahoo"):
                    pw = re.sub(r"\s", "", pw)              # Google shows it as four groups of four
                bad = app_password_problem(m.get("preset", ""), pw)
                if bad:
                    return self._json({"ok": False, "error": bad}, 400)
                m["password"] = pw
            if d.get("password") or before != (m.get("preset"), m.get("smtp_host"), m.get("smtp_port"), m.get("user")):
                m.pop("signin", None); m.pop("signin_error", None)     # new settings: not tried yet
            save_mail(m)
            return self._json({"ok": True} | mail_public())
        if u.path == "/api/update-check":
            try:
                return self._json(update_check())
            except Exception as e:  # noqa
                return self._json({"ok": False, "message": f"Couldn't check for updates ({type(e).__name__}: {str(e)[:120]}). Check the internet connection."})
        if u.path == "/api/update":
            try:
                return self._json(install_update())
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)})
            except Exception as e:  # noqa
                return self._json({"ok": False, "error": f"The update didn't install ({type(e).__name__}: {str(e)[:160]}). Your current version keeps working."})
        if u.path == "/api/mail-test":
            try:
                with smtp_connect(mail_settings()):
                    pass
                note_signin(True)
                return self._json({"ok": True, "message": "Signed in to the mail server - sending will work."})
            except Exception as e:
                text = mail_error(e, mail_settings())[0]
                if isinstance(e, smtplib.SMTPAuthenticationError):
                    note_signin(False, text)
                return self._json({"ok": False, "message": text})
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
                return self._json(mail_fail(e))
        if u.path == "/api/manual-draft":
            try:
                return self._json({"ok": True} | manual_draft(str(d.get("company", "")), str(d.get("position", "")), str(d.get("to", "")),
                                                               str(d.get("country", "")), str(d.get("ad", ""))[:20000]))
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)})
        if u.path == "/api/send-manual":
            try:
                return self._json(send_manual(str(d["company"]), str(d["position"]), str(d["to"]), str(d["subject"]), str(d["body"]),
                                              str(d.get("country", "")), str(d.get("ad", ""))[:20000]))
            except Exception as e:
                return self._json(mail_fail(e))
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
        if u.path == "/api/quit":
            if STATE["running"] and not d.get("force"):
                return self._json({"ok": False, "error": "A search is running - wait for it to finish, or quit anyway."})
            if any(not getattr(s_, "stop", True) for s_ in APPLY.values()) and not d.get("force"):
                return self._json({"ok": False, "error": "The apply window is still open - finish there first, or quit anyway."})
            threading.Thread(target=quit_app, daemon=True).start()
            return self._json({"ok": True})
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
    """At most 32 requests at a time: another program on this PC can't tie up the app by opening connections.
    On Windows the port is taken exclusively: a second copy of the app can't quietly bind the same port (Windows lets
    two programs listen on one port when both use SO_REUSEADDR - requests then land on either copy)."""
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(*a, **kw)

    def server_bind(self):
        if os.name == "nt":
            import socket as _socket
            flag = getattr(_socket, "SO_EXCLUSIVEADDRUSE", None)
            if flag is not None:
                self.socket.setsockopt(_socket.SOL_SOCKET, flag, 1)
        super().server_bind()

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


def quit_app(delay: float = 0.4) -> None:
    """Stop the app: close the browsers it started and end the process (the data folder is already saved)."""
    time.sleep(delay)
    _shutdown()
    try:
        from agent import sitebrowse
        sitebrowse.close()
    except Exception:
        pass
    os._exit(0)


def _idle_watch() -> None:
    """With the page closed for hours, no search running and no apply window open, the app quits by itself - the daily
    search is a separate scheduled task, so nothing needs the app running in the background."""
    while True:
        time.sleep(60)
        quiet = time.time() - LAST_POLL[0] > IDLE_QUIT
        busy = STATE["running"] or any(not getattr(s_, "stop", True) for s_ in APPLY.values())
        if quiet and not busy:
            quit_app(0)


def already_running() -> str:
    """What is on our port: 'same' (this build of the app), 'older' (another build of the app), or '' (something else)."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=3) as r:
            page = r.read(200_000)
    except Exception:
        return ""
    if b"<title>Job Agent</title>" not in page:
        return ""
    m = re.search(rb'name="jobagent-build" content="([^"]*)"', page)
    return "same" if m and m.group(1).decode(errors="replace") == BUILD else "older"


def _listening_pids(port: int) -> set[int]:
    """Processes listening on a local port (Windows)."""
    try:
        out = _run_quiet(["netstat", "-ano", "-p", "TCP"], timeout=20).stdout
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    return pids - {os.getpid()}


def stop_older_copy() -> bool:
    """An older copy of the app is still running on the port (there was no Quit button before): stop it so this one
    can start - unless it is in the middle of a search."""
    if os.name != "nt":
        return False
    lock = DATA / RUN_LOCK
    if lock.exists() and time.time() - lock.stat().st_mtime < 3 * 3600:
        return False
    pids = _listening_pids(PORT)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(20):
        if not _listening_pids(PORT):
            return True
        time.sleep(0.5)
    return not _listening_pids(PORT)


def serve(open_browser=True):
    sys.path.insert(0, str(BUNDLE))
    import atexit
    atexit.register(_shutdown)
    srv = AppServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(f"Job Agent running at {url}  (data in {DATA})")
    threading.Thread(target=_idle_watch, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="search once and exit (used by the scheduled task)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    RESTORED = None
    if os.environ.get("JOBAGENT_DATA"):
        DATA = Path(os.environ["JOBAGENT_DATA"])
    elif FROZEN:
        DATA = locate_data(HOME)
        RESTORED = restore_if_missing(DATA)
        if RESTORED:
            INFO["restored"] = RESTORED.name[:10]
    threading.Thread(target=tidy_after_update, daemon=True).start()
    try:
        ensure_data(background=not a.run)
        if FROZEN and not os.environ.get("JOBAGENT_DATA"):
            threading.Thread(target=backup_data, args=(DATA,), daemon=True).start()
    except Exception as e:  # noqa - e.g. the exe sits in a folder that can't be written (Program Files, a CD, a locked share)
        alert(f"Job Agent can't create its data folder next to the program:\n{DATA}\n\n{type(e).__name__}: {e}\n\n"
              "Move JobAgent.exe to a folder you can write to (for example Documents\\JobAgent) and start it again.")
        sys.exit(1)
    sys.path.insert(0, str(BUNDLE))
    if a.run:
        run_agent(block=True)
        print(STATE["summary"])
    else:
        try:
            serve(open_browser=not a.no_browser)
        except OSError as e:
            what = already_running()
            if what == "same":
                if not a.no_browser:
                    webbrowser.open(f"http://127.0.0.1:{PORT}/")   # already running: just open its page
            elif what == "older" and stop_older_copy():
                try:
                    serve(open_browser=not a.no_browser)       # the older copy is gone: start this one
                except OSError as e2:
                    alert(f"Job Agent couldn't take over port {PORT} from the older copy ({e2}). Restart the PC and try again.")
                    sys.exit(1)
            elif what == "older":
                alert("An older copy of Job Agent is still running and is in the middle of a search. Wait for it to finish "
                      "(or end JobAgent.exe in Task Manager), then start this one again.")
                sys.exit(1)
            else:
                alert(f"Job Agent can't start: another program is using port {PORT} on this PC ({e}).\n\n"
                      "Close that program (or restart the PC) and try again.")
                sys.exit(1)
