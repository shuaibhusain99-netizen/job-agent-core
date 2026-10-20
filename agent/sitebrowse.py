"""Job boards that refuse plain downloads - Indeed, Bayt, NaukriGulf, GulfTalent, Job Bank Canada - read through a real
browser on this computer, the way a person browses: a few result pages per search, one site at a time per tab, with pauses.

What it does: opens the site's own search page, reads the job list that is on the page, and (for the best matches) opens
the job's page to read the full ad. What it never does: click, type, sign in, fill a form, or apply. If a site shows a
"verify you are human" check, that site is skipped for this run - the check is never answered automatically.

The browser uses its own profile folder (data/browser-search), separate from the apply helper's, and its window is
placed off-screen so it doesn't get in your way (sources.json -> browser_sites.show_window: true to watch it).
"""
from __future__ import annotations
import json, os, random, re, subprocess, threading, time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote_plus

from .util import job, clean, log, guess_country, countries

PROFILE_DIR = Path("browser-search")          # set by main to <data>/browser-search

DEFAULT_QUERIES = ["civil engineer", "site engineer", "graduate civil engineer", "quantity surveyor", "QA QC engineer",
                   "planning engineer", "HSE officer", "document controller"]

GULF_SLUG = {"Saudi Arabia": "saudi-arabia", "United Arab Emirates": "uae", "Qatar": "qatar", "Kuwait": "kuwait", "Oman": "oman"}
INDEED_HOST = {"Saudi Arabia": "sa.indeed.com", "United Arab Emirates": "ae.indeed.com", "Qatar": "qa.indeed.com",
               "Kuwait": "kw.indeed.com", "Oman": "om.indeed.com", "Canada": "ca.indeed.com", "Australia": "au.indeed.com"}


def _slug(q: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")


# ------------------------------------------------------------------ what each site's result page looks like
INDEED_JS = r"""(() => {
  const d = window.mosaic && window.mosaic.providerData && window.mosaic.providerData["mosaic-provider-jobcards"];
  const rs = (((d || {}).metaData || {}).mosaicProviderJobCardsModel || {}).results || [];
  return rs.slice(0, 60).map(r => ({
    title: r.displayTitle || r.title || "", company: r.company || "", location: r.formattedLocation || "",
    url: r.jobkey ? location.origin + "/viewjob?jk=" + encodeURIComponent(r.jobkey) : "",
    snippet: String(r.snippet || "").replace(/<[^>]+>/g, " "),
    salary: (r.salarySnippet && r.salarySnippet.text) || "",
    posted: r.pubDate ? new Date(r.pubDate).toISOString().slice(0, 10) : "", age: r.formattedRelativeTime || "",
    expired: !!r.expired }));
})()"""

BAYT_JS = r"""[...document.querySelectorAll('li[data-js-job]')].slice(0, 60).map(c => {
  const a = c.querySelector('h2 a[href]'), w = c.querySelector('.job-company-location-wrapper');
  const comp = w && w.querySelector('a');
  const spans = w ? [...w.querySelectorAll('span')].map(s => s.textContent.trim()).filter(Boolean) : [];
  const dt = c.querySelector('[data-automation-id="job-active-date"]');
  return { title: a ? a.textContent.trim() : "", url: a ? a.href : "", company: comp ? comp.textContent.trim() : "",
           location: spans.join(", "), snippet: (c.innerText || c.textContent).replace(/\s+/g, " ").trim().slice(0, 700),
           age: dt ? dt.textContent.trim() : "" };
})"""

NAUKRI_JS = r"""[...document.querySelectorAll('.srp-tuple')].slice(0, 60).map(c => {
  const a = c.querySelector('a[href*="-jid-"]'), t = c.querySelector('.designation-title'), org = c.querySelector('.info-org');
  const spans = [...c.querySelectorAll('span')].map(s => s.textContent.trim());
  const exp = spans.find(s => /\byears?\b/i.test(s)) || "";
  const loc = spans.find(s => / - /.test(s) && !/\byears?\b/i.test(s)) || "";
  const desc = (c.querySelector('.description') || {}).textContent || "";
  return { title: (t || a || {}).textContent ? (t || a).textContent.trim() : "", url: a ? a.href : "",
           company: org ? org.textContent.trim() : "", location: loc,
           snippet: desc.trim() + (exp ? " Experience: " + exp + "." : ""), age: ((c.querySelector('.time') || {}).textContent || "").trim() };
})"""

GULFTALENT_JS = r"""[...document.querySelectorAll('a.ga-job-click')].slice(0, 60).map(a => {
  const tr = a.closest('tr') || a.parentElement;
  const cells = tr ? [...tr.children].map(td => td.textContent.replace(/\s+/g, ' ').trim()) : [];
  const comp = tr && [...tr.querySelectorAll('a')].find(x => x !== a && !/\/jobs\//.test(x.getAttribute('href') || ''));
  let di = -1;
  cells.forEach((c, i) => { if (/^\d{1,2} [A-Za-z]{3}/.test(c)) di = i; });           // "29 Sep"; the cell before it is the place
  return { title: a.textContent.trim(), url: a.href, company: comp ? comp.textContent.trim() : "",
           location: di > 0 ? cells[di - 1] : "", age: di >= 0 ? cells[di] : "", snippet: "" };
})"""

JOBBANK_JS = r"""[...document.querySelectorAll('article')].slice(0, 60).map(a => {
  const l = a.querySelector('a[href*="/jobposting/"]');
  const g = s => ((a.querySelector(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  return { title: g('.noctitle'), url: l ? l.href.replace(/;jsessionid=[^?]*/, '').replace(/\?.*$/, '') : "", company: g('.business'),
           location: g('.location').replace(/^Location\s*/, ''), age: g('.date'), salary: g('.salary').replace(/^Salary\s*/, ''), snippet: "" };
})"""

# full ad on a job's own page: the JobPosting data most boards publish for search engines, else the biggest text block
DETAIL_JS = r"""(() => {
  const strip = h => { const d = document.createElement('div'); d.innerHTML = String(h || ''); return d.textContent.replace(/\s+/g, ' ').trim(); };
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    let x; try { x = JSON.parse(s.textContent); } catch (e) { continue; }
    const items = Array.isArray(x) ? x : (x['@graph'] || [x]);
    for (const it of items) if (it && it['@type'] === 'JobPosting') {
      const exp = it.experienceRequirements && it.experienceRequirements.monthsOfExperience;
      return { description: strip(it.description) + (exp ? ' Experience required: ' + Math.round(exp / 12) + ' years.' : ''),
               posted: String(it.datePosted || '').slice(0, 10), valid: String(it.validThrough || '') };
    }
  }
  // no structured data: the page's text without scripts, menus, header/footer and forms, cut before "similar jobs" lists
  const root = (document.querySelector('main') || document.body).cloneNode(true);
  root.querySelectorAll('script,style,noscript,template,nav,header,footer,aside,form,iframe,svg,button').forEach(e => e.remove());
  let t = root.textContent.replace(/\s+/g, ' ').trim();
  const m = t.slice(300).search(/similar jobs|related jobs|jobs you may|recommended jobs|your feedback|more jobs (at|from|like)/i);
  if (m >= 0) t = t.slice(0, 300 + m);
  return { description: t.slice(0, 12000), posted: '', valid: '' };
})()"""

CHALLENGE_JS = r"""(() => /just a moment|security check|attention required|verify you are human|are you a robot|access denied|pardon our interruption/i.test(document.title)
  || !!document.querySelector('iframe[src*="challenges.cloudflare.com"], #challenge-form, #cf-challenge-running, iframe[src*="captcha-delivery"], #px-captcha'))()"""


SITES = {
    "indeed": {"name": "Indeed", "js": INDEED_JS, "detail": False,           # Indeed asks for a human check on job pages
               "where": lambda c: INDEED_HOST.get(c),
               "url": lambda w, q, p: f"https://{w}/jobs?q={quote_plus(q)}&sort=date&fromage=21" + (f"&start={p * 10}" if p else "")},
    "bayt": {"name": "Bayt", "js": BAYT_JS, "detail": True, "where": lambda c: GULF_SLUG.get(c),
             "url": lambda w, q, p: f"https://www.bayt.com/en/{w}/jobs/{_slug(q)}-jobs/" + (f"?page={p + 1}" if p else "")},
    "naukrigulf": {"name": "NaukriGulf", "js": NAUKRI_JS, "detail": True, "where": lambda c: GULF_SLUG.get(c),
                   "url": lambda w, q, p: f"https://www.naukrigulf.com/{_slug(q)}-jobs-in-{w}" + (f"-{p + 1}" if p else "") + "?sort=date"},
    "gulftalent": {"name": "GulfTalent", "js": GULFTALENT_JS, "detail": True, "where": lambda c: GULF_SLUG.get(c),
                   "url": lambda w, q, p: f"https://www.gulftalent.com/{w}/jobs/title/{_slug(q)}" + (f"/{p + 1}" if p else "")},
    "jobbank": {"name": "Job Bank", "js": JOBBANK_JS, "detail": True, "where": lambda c: "ca" if c == "Canada" else None,
                "url": lambda w, q, p: f"https://www.jobbank.gc.ca/jobsearch/jobsearch?searchstring={quote_plus(q)}&sort=D" + (f"&page={p + 1}" if p else "")},
}
SOURCE_NAMES = {s["name"] for s in SITES.values()}
DETAIL_SOURCES = {s["name"] for s in SITES.values() if s["detail"]}


# ------------------------------------------------------------------ dates like "2 days ago", "Just posted", "9 Jun", "October 19, 2026"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_age(text: str, today: date | None = None) -> str:
    t = (text or "").strip().lower()
    today = today or date.today()
    if not t:
        return ""
    if re.search(r"just|today|now|hour|min|sec|new", t):
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*(day|week|month)", t)
    if m:
        n = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        return (today - timedelta(days=n)).isoformat()
    m = re.search(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", t) or None          # October 19, 2026
    if m and m.group(1) in MONTHS:
        try:
            return date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))).isoformat()
        except ValueError:
            return ""
    m = re.search(r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?(?:\s+(\d{4}))?", t)                # 9 Jun / 9 June 2026
    if m and m.group(2) in MONTHS:
        try:
            d = date(int(m.group(3) or today.year), MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return ""
        if d > today + timedelta(days=1):
            d = d.replace(year=d.year - 1)
        return d.isoformat()
    return ""


# ------------------------------------------------------------------ our own browser, separate from the apply helper's
class Session:
    def __init__(self, show: bool = False):
        self.show = show
        self.proc = None
        self.port = 0
        self.lock = threading.Lock()
        self.tabs: list = []
        self.local = threading.local()

    def start(self) -> int:
        from .browser import find_browser, _free_port, _version
        with self.lock:
            if self.proc is not None and self.proc.poll() is None and _version(self.port):
                return self.port
            exe = find_browser()
            if not exe:
                raise RuntimeError("no Edge or Chrome found")
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            port = _free_port()
            args = [exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", f"--user-data-dir={PROFILE_DIR}",
                    "--no-first-run", "--no-default-browser-check", "--disable-features=TranslateUI", "--window-size=1280,900",
                    "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                    "--new-window", "about:blank"]
            if not self.show:
                args.insert(-1, "--window-position=-2400,-2400")
            if os.name != "nt" and not os.environ.get("DISPLAY"):
                args[-1:-1] = ["--headless=new", "--no-sandbox"]          # only for testing on a machine without a screen
            kw = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {}
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
            for _ in range(60):
                if _version(port):
                    self.port = port
                    return port
                if self.proc.poll() is not None:
                    break
                time.sleep(0.5)
            raise RuntimeError("the search browser did not start (is it already open with the same profile?)")

    def tab(self):
        """One tab per thread, so each website is read in its own tab."""
        from .browser import Tab, open_tab
        t = getattr(self.local, "tab", None)
        if t is not None and not t.closed:
            return t
        info = open_tab(self.start())
        t = Tab(info["webSocketDebuggerUrl"])
        t.target = info.get("id")
        self.local.tab = t
        with self.lock:
            self.tabs.append(t)
        return t

    def close(self):
        for t in self.tabs:
            try:
                t.close()
            except Exception:
                pass
        self.tabs = []
        if self.port:
            try:
                from .browser import WS, _version
                v = _version(self.port)
                if v and v.get("webSocketDebuggerUrl"):
                    ws = WS(v["webSocketDebuggerUrl"], timeout=3)
                    ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                    ws.close()
            except Exception:
                pass
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        self.proc, self.port = None, 0


SESSION: Session | None = None


def _session(cfg: dict | None = None) -> Session:
    global SESSION
    if SESSION is None:
        SESSION = Session(show=bool((cfg or {}).get("show_window")))
    return SESSION


def close() -> None:
    global SESSION
    if SESSION is not None:
        SESSION.close()
        SESSION = None


def _eval(tab, expr: str, timeout: float = 20):
    r = tab.call("Runtime.evaluate", timeout=timeout, expression=expr, returnByValue=True, awaitPromise=True)
    if r.get("exceptionDetails"):
        raise RuntimeError("page script failed")
    return (r.get("result") or {}).get("value")


def _goto(tab, url: str, settle: float = 2.0, timeout: float = 35) -> None:
    tab.call("Page.navigate", url=url, timeout=timeout)
    end = time.time() + timeout
    time.sleep(1.0)
    while time.time() < end:
        try:
            if _eval(tab, "document.readyState", timeout=5) == "complete":
                break
        except Exception:
            pass
        time.sleep(0.7)
    time.sleep(settle)


class Blocked(Exception):
    pass


def _check(tab, name: str) -> None:
    try:
        blocked = _eval(tab, CHALLENGE_JS, timeout=5)
    except Exception:
        blocked = False
    if blocked:
        time.sleep(6)                                    # some checks pass by themselves after a few seconds
        try:
            blocked = _eval(tab, CHALLENGE_JS, timeout=5)
        except Exception:
            pass
    if blocked:
        raise Blocked(name)


# ------------------------------------------------------------------ search
def _read_site(key: str, cfg: dict, profile: dict, sess: Session) -> list[dict]:
    site = SITES[key]
    name = site["name"]
    queries = cfg.get("queries") or DEFAULT_QUERIES
    pages = max(1, int(cfg.get("pages", 1)))
    out, seen = [], set()
    tab = sess.tab()
    try:
        for country in countries(profile):
            where = site["where"](country)
            if not where:
                continue
            for q in queries:
                for p in range(pages):
                    _goto(tab, site["url"](where, q, p))
                    _check(tab, name)
                    rows = _eval(tab, site["js"]) or []
                    if not isinstance(rows, list):
                        rows = []
                    new = 0
                    for r in rows[:60]:
                        if not isinstance(r, dict):
                            continue
                        url = str(r.get("url") or "")[:500]
                        if not url.startswith("https://") or url in seen or r.get("expired"):
                            continue
                        seen.add(url)
                        new += 1
                        loc = str(r.get("location") or "")[:120]
                        c = guess_country(loc)
                        c = country if c in ("", country) else c
                        if c not in countries(profile):
                            continue
                        text = " ".join(str(r.get(k) or "") for k in ("snippet", "salary"))[:1500]
                        posted = str(r.get("posted") or "")[:10] or parse_age(str(r.get("age") or "")[:40])
                        j = job(str(r.get("title") or "")[:200], str(r.get("company") or "")[:120], loc or country, url,
                                clean(text), name, c, posted)
                        if site["detail"]:
                            j["_snippet"] = True             # only the list's summary so far: worth opening the full ad
                        out.append(j)
                    if new < 10:
                        break
                    time.sleep(random.uniform(1.5, 3.0))
                time.sleep(random.uniform(1.5, 3.5))
    except Blocked:
        log(f"{name}: the site asked to verify you are human - skipped for this run ({len(seen)} jobs read before that). "
            f"It usually passes by itself next time.")
    except Exception as e:
        log(f"{name}: stopped ({type(e).__name__}: {e})")
    log(f"{name}: {len(seen)} listings")
    return out


def fetch(cfg: dict, profile: dict) -> list[dict]:
    """All enabled boards, each in its own tab, side by side."""
    if not cfg.get("enabled", True):
        return []
    BLOCKED.clear()
    on = [k for k, v in (cfg.get("sites") or {k: True for k in SITES}).items() if v and k in SITES]
    if not on:
        return []
    sess = _session(cfg)
    try:
        sess.start()
    except Exception as e:
        log(f"Indeed / Bayt / NaukriGulf / GulfTalent / Job Bank skipped: {e}")
        return []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(on)) as ex:
        results = list(ex.map(lambda k: _read_site(k, cfg, profile, sess), on))
    return [j for r in results for j in r]


BLOCKED: set[str] = set()


def enrich(j: dict) -> None:
    """Open one job's page in the search browser and read the full ad (Bayt, NaukriGulf, GulfTalent, Job Bank)."""
    if j.get("source") not in DETAIL_SOURCES or SESSION is None or j["source"] in BLOCKED:
        return
    tab = SESSION.tab()
    _goto(tab, j["url"], settle=1.5)
    try:
        _check(tab, j["source"])
    except Blocked:
        BLOCKED.add(j["source"])
        log(f"{j['source']}: asked to verify you are human - full ads not read this run")
        return
    j.pop("_snippet", None)
    d = _eval(tab, DETAIL_JS) or {}
    text = clean(str(d.get("description") or ""))[:6000]
    if len(text) > len(j.get("description", "")):
        j["description"] = text
    if not j.get("posted") and re.match(r"\d{4}-\d{2}-\d{2}$", str(d.get("posted") or "")):
        j["posted"] = d["posted"]
    time.sleep(random.uniform(1.0, 2.5))
