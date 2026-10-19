"""Apply-on-site helper: drives the user's own browser (Edge/Chrome) over the DevTools protocol - standard library only.

The browser runs with its own profile folder (data/browser) so sign-ins to LinkedIn, Bayt, GulfTalent ... are remembered.
For a job we open its page in a tab, inject filler.js (fills the form fields it recognises, uploads the CV, shows a small
JobAgent bar) and re-inject it on every navigation. It never presses Submit - the person does.
"""
from __future__ import annotations
import base64, json, os, socket, struct, subprocess, sys, threading, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

PORT = 9333
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


# ------------------------------------------------------------------ CDP session on one tab
class Tab:
    def __init__(self, ws_url: str):
        self.ws = WS(ws_url)
        self.n = 0
        self.lock = threading.Lock()

    def call(self, method: str, timeout: float = 30, **params):
        with self.lock:
            self.n += 1
            mid = self.n
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            self.ws.sock.settimeout(timeout)
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                    return msg.get("result", {})

    def eval(self, expr: str, timeout: float = 30):
        r = self.call("Runtime.evaluate", timeout=timeout, expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def close(self):
        self.ws.close()


# ------------------------------------------------------------------ browser process
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


def _alive() -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2).read()
        return True
    except Exception:
        return False


def launch(profile_dir: Path, headless: bool = False) -> str:
    """Start the browser with remote debugging if it isn't running. Returns the executable used."""
    if _alive():
        return "running"
    exe = find_browser()
    if not exe:
        raise RuntimeError("No browser found. Install Microsoft Edge or Google Chrome.")
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [exe, f"--remote-debugging-port={PORT}", f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
            "--disable-features=TranslateUI", "--new-window", "about:blank"]
    if headless:
        args += ["--headless=new", "--disable-gpu", "--no-sandbox"]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    for _ in range(60):
        if _alive():
            return exe
        time.sleep(0.5)
    raise RuntimeError("Browser did not start")


def open_tab(url: str) -> dict:
    body = urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?{url}", method="PUT"), timeout=10).read()
    return json.loads(body)


def tabs() -> list[dict]:
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5).read())


# ------------------------------------------------------------------ the filler
def filler_script(profile: dict, job: dict, app_url: str) -> str:
    js = (HERE / "filler.js").read_text(encoding="utf-8")
    ctx = {"profile": profile, "job": {k: job.get(k, "") for k in ("url", "title", "company", "country", "location", "apply_email")},
           "cover_letter": job.get("cover_letter", ""), "app": app_url}
    return js.replace("__JOBAGENT_CONTEXT__", json.dumps(ctx, ensure_ascii=False))


class ApplySession:
    """One job open in one tab: injects the filler now and on every navigation, uploads the CV when a file input appears."""

    def __init__(self, url: str, profile: dict, job: dict, cv_path: Path | None, app_url: str, headless: bool = False, profile_dir: Path | None = None):
        self.url, self.profile, self.job, self.cv, self.app_url = url, profile, job, cv_path, app_url
        launch(profile_dir or Path("browser"), headless=headless)
        info = open_tab(url)
        self.tab_id = info["id"]
        self.tab = Tab(info["webSocketDebuggerUrl"])
        self.tab.call("Page.enable")
        self.tab.call("Runtime.enable")
        self.tab.call("DOM.enable")
        try:
            self.tab.call("Emulation.setFocusEmulationEnabled", enabled=True)   # focus events even when the window isn't foreground
        except Exception:
            pass
        script = filler_script(profile, job, app_url)
        self.tab.call("Page.addScriptToEvaluateOnNewDocument", source=script)
        self.script = script
        self.stop = False
        self.log: list[str] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        time.sleep(1.5)
        last_fill = 0
        while not self.stop:
            try:
                # the filler is a no-op when it already ran on this document; re-run picks up fields that appeared later
                res = self.tab.eval("(window.__jobagent && window.__jobagent.run()) || 'no-filler'", timeout=15)
                if res == "no-filler":
                    self.tab.eval(self.script, timeout=15)
                    res = self.tab.eval("window.__jobagent && window.__jobagent.run()", timeout=15)
                if isinstance(res, dict) and res.get("filled"):
                    last_fill = time.time()
                    self.log.append(f"filled {res['filled']} field(s)")
                if self.cv and self.cv.exists():
                    self._upload_cv()
                self._combos()
                if isinstance(res, dict) and res.get("submitted"):
                    self.log.append("submitted")
                    self.stop = True
            except Exception as e:  # tab closed etc.
                self.log.append(f"stopped: {e}")
                if "closed" in str(e).lower() or "No target" in str(e):
                    self.stop = True
            time.sleep(2.5)
        try:
            self.tab.close()
        except Exception:
            pass

    def _click(self, nid: int):
        obj = self.tab.call("DOM.resolveNode", nodeId=nid)["object"]["objectId"]
        self.tab.call("Runtime.callFunctionOn", objectId=obj, functionDeclaration="function(){document.documentElement.style.scrollBehavior='auto';this.scrollIntoView({block:'center',behavior:'instant'})}")
        time.sleep(0.25)
        q = self.tab.call("DOM.getContentQuads", nodeId=nid)["quads"][0]
        x, y = (q[0] + q[2]) / 2, (q[1] + q[5]) / 2
        self.tab.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        self.tab.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
        self.tab.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)

    def _key(self, key: str, code: int):
        self.tab.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=key, windowsVirtualKeyCode=code)
        self.tab.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=key, windowsVirtualKeyCode=code)

    def _combos(self):
        """Dropdown-style inputs (react-select, Workday, LinkedIn typeaheads): click, type the value, pick the matching option."""
        for _ in range(6):
            doc = self.tab.call("DOM.getDocument", depth=-1, pierce=True)
            nid = self.tab.call("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector="[data-jobagent-combo]").get("nodeId")
            if not nid:
                break
            try:
                attrs = self.tab.call("DOM.getAttributes", nodeId=nid)["attributes"]
                want = attrs[attrs.index("data-jobagent-combo") + 1]
                self.tab.call("DOM.removeAttribute", nodeId=nid, name="data-jobagent-combo")
                self._key("Escape", 27)
                self._click(nid)
                time.sleep(0.5)
                opened = self.tab.eval("(()=>{const e=document.activeElement;return !!(e&&(e.getAttribute('aria-expanded')==='true'||document.querySelector('[role=listbox]:not([class*=iti]) [role=option],[class*=\"select__option\"],[class*=\"menu\"] [class*=\"option\"]')))})()")
                if not opened:
                    self._click(nid); time.sleep(0.5)
                self.tab.call("Input.insertText", text=want[:40])
                time.sleep(0.9)
                w = json.dumps(want.lower())
                hit = self.tab.eval(f"""(()=>{{const w={w};const vis=e=>{{const r=e.getBoundingClientRect();return r.width>0&&r.height>0}};
                    const notPhone=o=>!o.closest('[class*="iti"],[class*="flag"],[class*="dial"]');
                    let opts=[...document.querySelectorAll('[role="option"]')].filter(o=>vis(o)&&notPhone(o));
                    if(!opts.length) opts=[...document.querySelectorAll('[class*="option"],[class*="Option"]')].filter(o=>vis(o)&&notPhone(o)&&!o.querySelector('input')&&o.textContent.trim().length<120);
                    const t=opts.map(o=>({{o,t:o.textContent.replace(/\\s+/g,' ').trim().toLowerCase()}}));
                    const yn=(w==='yes'||w==='no');
                    const sw=t.filter(x=>x.t===w||x.t.startsWith(w+' ')||x.t.startsWith(w+',')||x.t.startsWith(w+' -')||x.t===w+'.').sort((a,b)=>a.t.length-b.t.length);
                    let h=t.find(x=>x.t===w)||null;
                    if(!h&&sw.length===1) h=sw[0]; else if(!h&&sw.length>1&&yn) return 'AMBIGUOUS';
                    if(!h&&!yn) h=t.find(x=>x.t.startsWith(w))||t.find(x=>x.t.includes(w));
                    if(!h) return opts.length?null:'NOMENU'; h.o.setAttribute('data-jobagent-pick','1'); return h.t;}})()""")
                if hit in ("AMBIGUOUS", "NOMENU", None):
                    if os.environ.get("JOBAGENT_DEBUG"):
                        self.log.append("options seen: " + str(self.tab.eval("[...document.querySelectorAll('[role=option],[class*=option]')].filter(o=>{const r=o.getBoundingClientRect();return r.width>0}).map(o=>o.textContent.trim().slice(0,30)).slice(0,8)"))
                                        + " active=" + str(self.tab.eval("(document.activeElement||{}).id")))
                    self._key("Escape", 27)
                    self.log.append(f"left for you: '{want}' ({'several options match' if hit == 'AMBIGUOUS' else 'no matching option'})")
                    time.sleep(0.3)
                    continue
                if hit:
                    doc = self.tab.call("DOM.getDocument", depth=-1, pierce=True)
                    pick = self.tab.call("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector="[data-jobagent-pick]")["nodeId"]
                    self._click(pick)
                    self.tab.eval("document.querySelectorAll('[data-jobagent-pick]').forEach(e=>e.removeAttribute('data-jobagent-pick'))")
                self.log.append(f"chose '{hit or want}'")
                time.sleep(0.4)
            except Exception as e:
                self.log.append(f"dropdown skipped: {str(e)[:60]}")

    def _upload_cv(self):
        """Set the CV on every empty file input (JS can't do this; DevTools can)."""
        doc = self.tab.call("DOM.getDocument", depth=-1, pierce=True)
        ids = self.tab.call("DOM.querySelectorAll", nodeId=doc["root"]["nodeId"], selector="input[type=file]:not([data-jobagent-cv])").get("nodeIds", [])
        for nid in ids:
            try:
                self.tab.call("DOM.setFileInputFiles", nodeId=nid, files=[str(self.cv)])
                self.tab.call("DOM.setAttributeValue", nodeId=nid, name="data-jobagent-cv", value="1")
                self.log.append("CV attached")
            except Exception:
                pass
