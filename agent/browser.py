"""Apply-on-site helper: drives a browser (Edge/Chrome) over the DevTools protocol - standard library only.

The browser runs with its own profile folder (data/browser) so sign-ins to LinkedIn, Bayt, GulfTalent ... are remembered.
For a job we open its page in a tab and run filler.js in an ISOLATED WORLD (like a password manager): the page cannot read
the profile, the cover letter or the filler's state, and cannot tamper with the script's built-ins.
Safety rules (see filler.js): auto-fill only on the job's own site and known job boards; only fields actually on screen;
the CV is attached only after the person clicks "Attach CV"; consent / legal declarations are never ticked; nothing is
ever submitted - the person presses the site's own button.
"""
from __future__ import annotations
import base64, json, os, socket, struct, subprocess, threading, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .util import fill_in

HERE = Path(__file__).resolve().parent


# ------------------------------------------------------------------ tiny WebSocket client (RFC 6455, client side)
class WS:
    def __init__(self, url: str, timeout: float = 20):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError("websocket handshake refused: " + resp[:80].decode(errors="replace"))
        self.buf = resp.split(b"\r\n\r\n", 1)[1]
        self.lock = threading.Lock()

    def send(self, text: str):
        data = text.encode()
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126); head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127); head += struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.lock:
            self.sock.sendall(bytes(head) + mask + masked)

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> str:
        msg = b""
        while True:
            b1, b2 = self._read(2)
            op = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if b2 & 0x80:
                mask = self._read(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(n)))
            else:
                payload = self._read(n)
            if op == 0x8:
                raise ConnectionError("websocket closed by peer")
            if op == 0x9:          # ping -> pong
                self.send_frame(0xA, payload); continue
            if op in (0x1, 0x0, 0x2):
                msg += payload
                if b1 & 0x80:
                    return msg.decode(errors="replace")

    def send_frame(self, op: int, payload: bytes):
        head = bytearray([0x80 | op, 0x80 | len(payload)])
        mask = os.urandom(4)
        with self.lock:
            self.sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass



# ------------------------------------------------------------------ CDP session on one tab (responses + events)
class Tab:
    def __init__(self, ws_url: str):
        self.ws = WS(ws_url)
        self.ws.sock.settimeout(None)
        self.n = 0
        self.lock = threading.Lock()
        self.waiting: dict[int, dict] = {}
        self.cv = threading.Condition()
        self.listeners: list = []
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if "id" in msg:
                    with self.cv:
                        self.waiting[msg["id"]] = msg
                        self.cv.notify_all()
                else:
                    for fn in list(self.listeners):
                        try:
                            fn(msg.get("method"), msg.get("params") or {})
                        except Exception:
                            pass
        except Exception:
            self.closed = True
            with self.cv:
                self.cv.notify_all()

    def call(self, method: str, timeout: float = 30, **params):
        with self.lock:
            self.n += 1
            mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        end = time.time() + timeout
        with self.cv:
            while mid not in self.waiting:
                if self.closed:
                    raise ConnectionError("tab closed")
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError(method)
                self.cv.wait(left)
            msg = self.waiting.pop(mid)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result", {})

    def close(self):
        self.closed = True
        self.ws.close()


# ------------------------------------------------------------------ browser process
_STATE = {"port": 0, "proc": None}


def find_browser() -> str | None:
    env = os.environ.get("JOBAGENT_BROWSER")
    if env and Path(env).exists():
        return env
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("LOCALAPPDATA", "")):
            cands += [Path(base) / r"Microsoft\Edge\Application\msedge.exe", Path(base) / r"Google\Chrome\Application\chrome.exe",
                      Path(base) / r"BraveSoftware\Brave-Browser\Application\brave.exe"]
    else:
        cands += [Path("/opt/pw-browsers/chromium"), Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser"),
                  Path("/usr/bin/microsoft-edge")]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return None


def kill_tree(proc) -> None:
    """End a browser we started and every process it started (Edge/Chrome leave helper processes behind otherwise)."""
    if proc is None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _version(port: int) -> dict | None:
    try:
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read())
    except Exception:
        return None


def launch(profile_dir: Path, headless: bool = False) -> int:
    """Start our own browser on a random local debugging port (never re-use a browser we did not start). Returns the port."""
    proc = _STATE["proc"]
    if proc is not None and proc.poll() is None and _version(_STATE["port"]):
        return _STATE["port"]
    exe = find_browser()
    if not exe:
        raise RuntimeError("No browser found. Install Microsoft Edge or Google Chrome.")
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    args = [exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile_dir}",
            "--no-first-run", "--no-default-browser-check", "--disable-features=TranslateUI", "--new-window", "about:blank"]
    if headless:
        args += ["--headless=new", "--disable-gpu", "--no-sandbox"]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    for _ in range(60):
        if _version(port):
            _STATE.update(port=port, proc=proc)
            return port
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    raise RuntimeError("The browser did not start. If it is already open with this profile, close it and try again.")


def close_browser():
    """Close the browser we started (called when the app exits) so its debugging port doesn't stay open."""
    v = _version(_STATE["port"]) if _STATE["port"] else None
    if v and v.get("webSocketDebuggerUrl"):
        try:
            ws = WS(v["webSocketDebuggerUrl"], timeout=3)
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            ws.close()
        except Exception:
            pass
    proc = _STATE["proc"]
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        kill_tree(proc)


def open_tab(port: int) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


# ------------------------------------------------------------------ the filler
TRUSTED_HOSTS = [
    "linkedin.com", "myworkdayjobs.com", "myworkdaysite.com", "workday.com", "successfactors.com", "successfactors.eu", "sapsf.com",
    "greenhouse.io", "lever.co", "workable.com", "smartrecruiters.com", "bayt.com", "gulftalent.com", "naukrigulf.com", "seek.com.au",
    "indeed.com", "taleo.net", "oraclecloud.com", "icims.com", "jobvite.com", "ashbyhq.com", "bamboohr.com", "recruitee.com",
    "teamtailor.com", "zohorecruit.com", "rigzone.com", "jobs.ca", "jobbank.gc.ca", "applytojob.com", "breezy.hr", "personio.de",
    "personio.com", "pinpointhq.com", "dayforcehcm.com", "ultipro.com", "ukg.com", "adp.com", "paylocity.com", "csod.com",
]


def _host_ok(url: str) -> bool:
    u = urlparse(url)
    return u.scheme in ("http", "https") and bool(u.hostname)


def filler_script(profile: dict, job: dict, years: int) -> str:
    js = (HERE / "filler.js").read_text(encoding="utf-8")
    answers = {k: fill_in(v, profile, years) for k, v in (profile.get("screening_answers") or {}).items()}
    ctx = {"profile": {"candidate": profile.get("candidate"), "form_answers": profile.get("form_answers"), "screening_answers": answers},
           "years": years,
           "job": {k: job.get(k, "") for k in ("title", "company", "country", "location")},
           "cover_letter": job.get("cover_letter", ""),
           "hosts": TRUSTED_HOSTS}          # the job's own site is NOT auto-filled: you press "Fill this page" there
    # JSON is a JS literal; escape "</" and U+2028/9 so it can never break out of the script
    lit = json.dumps(ctx, ensure_ascii=True).replace("</", "<\\/")
    return js.replace("__JOBAGENT_CONTEXT__", lit)


class ApplySession:
    """One job in one tab. The filler runs in an isolated world on every page of that tab; this loop polls its state,
    attaches the CV when the person asks, drives dropdowns with real input, and reports submission."""

    def __init__(self, url: str, profile: dict, job: dict, cv_path: Path | None, years: int, on_submitted=None,
                 headless: bool = False, profile_dir: Path | None = None):
        if not _host_ok(url):
            raise ValueError("Only http(s) job links can be opened")
        self.url, self.cv, self.on_submitted = url, cv_path, on_submitted
        self.world = "jobagent_" + base64.b32encode(os.urandom(10)).decode().lower().rstrip("=")
        port = launch(profile_dir or Path("browser"), headless=headless)
        info = open_tab(port)
        self.tab = Tab(info["webSocketDebuggerUrl"])
        self.ctx_id = None
        self.ctx_ids: list[int] = []          # isolated contexts of the main frame (Chrome may add empty ones)
        self.tab.listeners.append(self._on_event)
        self.tab.call("Page.enable")
        self.main_frame = self.tab.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        self.tab.call("Runtime.enable")
        self.tab.call("DOM.enable")
        try:
            self.tab.call("Emulation.setFocusEmulationEnabled", enabled=True)
        except Exception:
            pass
        self.tab.call("Page.addScriptToEvaluateOnNewDocument", source=filler_script(profile, job, years), worldName=self.world)
        self.tab.call("Page.navigate", url=url)
        self.stop = False
        self.done = False
        self.log: list[str] = []
        threading.Thread(target=self._loop, daemon=True).start()

    # --- isolated-world plumbing
    def _on_event(self, method, p):
        if method == "Runtime.executionContextCreated":
            c = p.get("context", {})
            aux = c.get("auxData") or {}
            if c.get("name") == self.world and aux.get("frameId") == self.main_frame:
                self.ctx_ids.append(c["id"])
        elif method == "Runtime.executionContextsCleared":
            self.ctx_id, self.ctx_ids = None, []
        elif method == "Runtime.executionContextDestroyed":
            cid = p.get("executionContextId")
            if cid in self.ctx_ids:
                self.ctx_ids.remove(cid)
            if cid == self.ctx_id:
                self.ctx_id = None
        elif method == "Inspector.detached" or method == "Target.targetDestroyed":
            self.stop = True

    def _find_ctx(self):
        """The isolated context where the filler actually runs (it defines __ja)."""
        for cid in reversed(list(self.ctx_ids)):
            try:
                r = self.tab.call("Runtime.evaluate", expression="typeof window.__ja", contextId=cid, returnByValue=True, timeout=5)
                if r.get("result", {}).get("value") == "object":
                    self.ctx_id = cid
                    return
            except Exception:
                continue

    def _eval(self, expr: str, by_value: bool = True, timeout: float = 15):
        if not self.ctx_id or self.ctx_id not in self.ctx_ids:
            self.ctx_id = None
            self._find_ctx()
        if not self.ctx_id:
            return None
        r = self.tab.call("Runtime.evaluate", timeout=timeout, expression=expr, contextId=self.ctx_id,
                          returnByValue=by_value, awaitPromise=True)
        res = r.get("result", {})
        return res.get("value") if by_value else res.get("objectId")

    def _on(self, obj: str, fn: str, *args):
        r = self.tab.call("Runtime.callFunctionOn", objectId=obj, functionDeclaration=fn,
                          arguments=[{"value": a} for a in args], returnByValue=True)
        return r.get("result", {}).get("value")

    # --- main loop
    def _loop(self):
        while not self.stop and not self.tab.closed:
            try:
                st = self._eval("window.__ja ? __ja.state() : null")
                if isinstance(st, dict):
                    if st.get("filledNow"):
                        self.log.append(f"filled {st['filledNow']} field(s)")
                    if st.get("cvRequested") and self.cv and self.cv.exists():
                        self._attach_cv()
                    if st.get("combos"):
                        self._combos()
                    if st.get("submitted") and not self.done:
                        self.done = True
                        self.log.append("submitted")
                        self.unmarked = bool(self.on_submitted)
                if getattr(self, "unmarked", False):            # keep trying while the list is busy / open in Excel
                    try:
                        self.on_submitted()
                        self.unmarked = False
                        self.log.append("marked as Applied in your list")
                    except Exception as e:  # noqa
                        if not getattr(self, "_warned", False):
                            self.log.append(f"couldn't mark it Applied yet ({type(e).__name__}) - will keep trying")
                            self._warned = True
            except (ConnectionError, OSError):
                break
            except Exception as e:
                self.log.append(f"note: {str(e)[:80]}")
            time.sleep(1.5)
        if getattr(self, "unmarked", False):
            try:
                self.on_submitted(); self.unmarked = False
            except Exception:  # noqa
                self.log.append("not marked Applied - the job list was busy; mark it yourself")
        self.stop = True
        if not self.done:
            self.log.append("window closed")
        try:
            self.tab.close()
        except Exception:
            pass

    def _attach_cv(self):
        n = 0
        for i in range(6):
            obj = self._eval(f"__ja.cvTarget({i})", by_value=False)
            if not obj:
                break
            try:
                self.tab.call("DOM.setFileInputFiles", objectId=obj, files=[str(self.cv)])
                self._on(obj, "function(){this.dispatchEvent(new Event('change',{bubbles:true}))}")
                n += 1
            except Exception as e:
                self.log.append(f"CV not attached: {str(e)[:60]}")
        self._eval(f"__ja.cvDone({n})")
        self.log.append(f"CV attached ({n})" if n else "no CV upload box found on this page")

    def _point(self, obj: str):
        self._on(obj, "function(){document.documentElement.style.scrollBehavior='auto';this.scrollIntoView({block:'center',behavior:'instant'})}")
        time.sleep(0.25)
        q = self.tab.call("DOM.getContentQuads", objectId=obj)["quads"][0]
        return (q[0] + q[2]) / 2, (q[1] + q[5]) / 2

    def _click(self, obj: str) -> bool:
        """Real mouse click on an element - only if the element (not something laid over it) is what's under the pointer."""
        x, y = self._point(obj)
        if self._on(obj, "function(){return !!this.closest('button,a,[type=submit],[role=button],[role=link]')}"):
            self.log.append("skipped a click: that element is a button or link")      # the helper never presses buttons
            return False
        check = ("function(x,y){const h=document.elementFromPoint(x,y);if(!h)return false;"
                 "if(h.closest('button,a,[type=submit],[type=image],[role=button],[role=link]'))return false;"   # never a button under the pointer
                 "const box=this.closest('[class*=control],[role=combobox],[class*=select]')||this;"
                 "return h===this||this.contains(h)||h.contains(this)&&h.getBoundingClientRect().width<=box.getBoundingClientRect().width*1.5||box.contains(h);}")
        if not self._on(obj, check, x, y):
            self.log.append("skipped a click: something was covering the field")
            return False
        self.tab.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none")
        time.sleep(0.15)
        if not self._on(obj, check, x, y):                  # the page moved something under the pointer: no click
            self.log.append("skipped a click: the page changed under the pointer")
            return False
        self._eval("__ja.guard()")                              # buttons/links/submits are cancelled for the next moment
        for t in ("mousePressed", "mouseReleased"):
            self.tab.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)
        return True

    def _key(self, key: str, code: int):
        self.tab.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=key, windowsVirtualKeyCode=code)
        self.tab.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=key, windowsVirtualKeyCode=code)

    def _combos(self):
        """Dropdown-style inputs (react-select, Workday, LinkedIn typeaheads): click, type the value, pick the exact option."""
        for _ in range(6):
            want = self._eval("__ja.nextCombo()")
            if not want:
                break
            el = self._eval("__ja.current()", by_value=False)
            if not el:
                continue
            try:
                self._key("Escape", 27)
                if not self._click(el):
                    continue
                time.sleep(0.5)
                if not self._eval("__ja.menuOpen()"):
                    self._click(el); time.sleep(0.5)
                self.tab.call("Input.insertText", text=str(want)[:40])
                time.sleep(0.9)
                code = self._eval(f"__ja.findOption({json.dumps(str(want))})")
                if code != "OK":
                    self._key("Escape", 27)
                    self.log.append(f"left for you: '{want}' ({'several options match' if code == 'AMBIGUOUS' else 'no matching option'})")
                    continue
                opt = self._eval("__ja.option()", by_value=False)
                if opt and self._click(opt):
                    self.log.append(f"chose '{want}'")
                time.sleep(0.4)
            except Exception as e:
                self.log.append(f"dropdown skipped: {str(e)[:60]}")
