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
import argparse, io, json, os, shutil, subprocess, sys, threading, time, webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

FROZEN = getattr(sys, "frozen", False)
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))      # read-only program files
HOME = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
DATA = HOME / "data"
PORT = 8765


def ensure_data():
    DATA.mkdir(exist_ok=True)
    (DATA / "applications").mkdir(exist_ok=True)
    for name in ("profile.json", "keys.json", "sources.json"):
        if not (DATA / name).exists():
            shutil.copy(BUNDLE / name, DATA / name)
    upgrade_config()
    if not (DATA / "tracker.csv").exists():
        (DATA / "tracker.csv").write_text("﻿found_date,score,verdict,title,company,location,country,source,url,status,applied_date,notes,pack_file,user_notes,category,sector,salary\n", encoding="utf-8")


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
            if changed:
                (DATA / name).write_text(json.dumps(user, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception as e:  # noqa
            print(f"config upgrade skipped for {name}: {e}")
    # older tracker files: add the new columns
    try:
        t = DATA / "tracker.csv"
        head = t.read_text(encoding="utf-8-sig").splitlines()[0] if t.exists() else ""
        if head and "sector" not in head:
            import csv, io
            rows = list(csv.DictReader(io.StringIO(t.read_text(encoding="utf-8-sig"))))
            fields = head.split(",") + ["sector", "salary"]
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


# ----------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code=200, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _safe(self, rel: str) -> Path | None:
        p = (DATA / rel).resolve()
        return p if str(p).startswith(str(DATA.resolve())) else None

    def do_GET(self):
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
        return self._json({"error": "no route"}, 404)

    def do_POST(self):
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
