"""Shared helpers: HTTP, text cleaning, job record shape."""
from __future__ import annotations
import html, json, re, sys, threading, time, zlib
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip"}
_LOG_LOCK = threading.Lock()


SECRET_IN_URL = re.compile(r"(jooble\.org/api/)[^\s/?#]+|([?&](?:key|api_key|apikey|token|access_token)=)[^\s&#]+", re.I)


def log(msg: str) -> None:
    msg = SECRET_IN_URL.sub(lambda m: (m.group(1) or m.group(2)) + "<key>", str(msg))    # keys never reach the log or the page
    with _LOG_LOCK:                       # sources run side by side: one whole line at a time
        try:
            print(msg, file=sys.stderr, flush=True)
        except Exception:                 # no console (the packaged app) - nothing to do
            pass


MAX_BYTES = 5 * 1024 * 1024


def safe_url(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname)


# ------------------------------------------------------------------ polite HTTP
# A site that says "slow down" (429) with a long Retry-After is left alone until then - remembered between searches in
# cooldown.json - and a site that fails three times in a row is skipped for the rest of this search, so a dead or
# blocking site never costs more than a few seconds and is never hammered.
COOLDOWN_FILE: Path | None = None        # set by main to <data>/cooldown.json
_COOL: dict[str, float] = {}
_FAILS: dict[str, int] = {}
_DEAD: set[str] = set()
_NET_LOCK = threading.Lock()


def load_cooldowns() -> None:
    _COOL.clear(); _FAILS.clear(); _DEAD.clear()
    try:
        d = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8")) if COOLDOWN_FILE else {}
        now = time.time()
        _COOL.update({h: float(t) for h, t in d.items() if float(t) > now})
    except Exception:
        pass


def save_cooldowns() -> None:
    if not COOLDOWN_FILE:
        return
    now = time.time()
    try:
        COOLDOWN_FILE.write_text(json.dumps({h: t for h, t in _COOL.items() if t > now}), encoding="utf-8")
    except OSError:
        pass


def site_key(url: str) -> str:
    """The website a link belongs to: 'linkedin.com' for sa.linkedin.com and www.linkedin.com, 'seek.com.au' ..."""
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("com", "co", "gov", "gc", "org", "net", "ac", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _failed(host: str, why: str) -> None:
    with _NET_LOCK:
        _FAILS[host] = _FAILS.get(host, 0) + 1
        if _FAILS[host] >= 3 and host not in _DEAD:
            _DEAD.add(host)
            log(f"  {host} isn't answering ({why}) - skipped for the rest of this search")


def _ok(host: str) -> None:
    with _NET_LOCK:
        _FAILS[host] = 0


def host_blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _DEAD or _COOL.get(host, 0) > time.time()


_TL = threading.local()


def last_status():
    """Status of this thread's last http() call (429 = the site asked us to slow down)."""
    return getattr(_TL, "status", None)


def reset_status() -> None:
    _TL.status = None


def http(url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 20, retries: int = 1) -> tuple[int, str]:
    st, body = _http(url, data, headers, timeout, retries)
    _TL.status = st
    return st, body


def _http(url: str, data: bytes | None = None, headers: dict | None = None,
          timeout: int = 20, retries: int = 1) -> tuple[int, str]:
    """GET (or POST when data given). Returns (status, body). Never raises. Only http(s); bodies capped at 5 MB.
    429 = the site asked us to slow down: one short wait at most, then the caller should stop asking."""
    if not safe_url(url):
        log(f"  refused non-web link {str(url)[:60]}")
        return 0, ""
    host = (urlparse(url).hostname or "").lower()
    if host in _DEAD:
        return 0, ""
    if _COOL.get(host, 0) > time.time():
        return 429, ""
    h = dict(BASE_HEADERS)
    if headers:
        h.update(headers)
    for attempt in range(retries + 1):
        try:
            req = Request(url, data=data, headers=h, method="POST" if data else "GET")
            with urlopen(req, timeout=timeout) as r:
                raw = r.read(MAX_BYTES + 1)
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    raw = d.decompress(raw, MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    log(f"  response too large, skipped {url[:80]}")
                    return 0, ""
                _ok(host)
                return r.status, raw.decode("utf-8", errors="replace")
        except HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
                if wait > 30 or attempt >= retries:
                    with _NET_LOCK:
                        _COOL[host] = time.time() + min(max(wait, 600), 2 * 86400)   # leave it alone for a while
                    log(f"  {host} asked to slow down - left alone for {int(min(max(wait, 600), 2 * 86400) // 60)} min")
                    _failed(host, "429")
                    return 429, ""
                time.sleep(max(wait, 5))
                continue
            if e.code >= 500:
                _failed(host, f"HTTP {e.code}")
            return e.code, ""
        except (URLError, TimeoutError, OSError, zlib.error) as e:
            if attempt < retries:
                time.sleep(2)
                continue
            log(f"  network error {url[:80]}: {e}")
            _failed(host, "network error")
            return 0, ""
    return 0, ""


def http_json(url: str, payload: dict | None = None, headers: dict | None = None):
    h = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    status, body = http(url, data=data, headers=h)
    if status != 200 or not body:
        return status, None
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, None


TAG = re.compile(r"</?[A-Za-z!][^<>]{0,800}>")


BREAKS = re.compile(r"<\s*(br|/p|/li|li|/div|/h[1-6]|/tr)\b[^>]*>", re.I)


def clean(value, keep_lines: bool = False) -> str:
    """Plain text from HTML: tags removed, entities decoded, and tags that were themselves escaped ('&lt;b&gt;') removed too.
    keep_lines: list items / paragraphs stay on their own lines (ads), so one requirement isn't glued to the next."""
    text = str(value or "")
    if keep_lines:
        text = BREAKS.sub("\n", text)
    text = html.unescape(TAG.sub(" ", text))
    if keep_lines:
        text = BREAKS.sub("\n", text)
        text = TAG.sub(" ", text)
        text = re.sub(r"[ \t\r\f\v\u00a0]+", " ", text)
        return re.sub(r"\s*\n\s*", "\n", text).strip()
    return re.sub(r"\s+", " ", TAG.sub(" ", text)).strip()


def job(title, company, location, url, description="", source="", country="", posted=""):
    return {
        "title": clean(title),
        "company": clean(company) or "Unknown",
        "location": clean(location) or "Not specified",
        "country": country or guess_country(clean(location)),
        "url": (url or "").strip() if safe_url(url) else "",
        "description": clean(description, keep_lines=True),
        "source": source,
        "posted": posted or "",
    }


# Country detection: explicit country names first, then provinces/states, then cities - all whole words.
TARGET_NAMES = {
    "Saudi Arabia": [r"saudi arabia", r"saudi", r"ksa", r"kingdom of saudi"],
    "United Arab Emirates": [r"united arab emirates", r"uae", r"u\.a\.e\.?"],
    "Qatar": [r"qatar"], "Kuwait": [r"kuwait"], "Oman": [r"oman", r"sultanate of oman"], "Bahrain": [r"bahrain"],
    "Canada": [r"canada"], "Australia": [r"australia"],
}
OTHER_NAMES = [r"united states", r"usa", r"u\.s\.a?\.?", r"united kingdom", r"uk", r"england", r"scotland", r"wales", r"ireland",
               r"india", r"pakistan", r"bangladesh", r"sri lanka", r"nepal", r"philippines", r"egypt", r"jordan", r"lebanon", r"iraq",
               r"iran", r"turkey", r"t[uü]rkiye", r"syria", r"yemen", r"romania", r"germany", r"france", r"spain", r"italy", r"netherlands",
               r"belgium", r"poland", r"norway", r"sweden", r"denmark", r"switzerland", r"austria", r"portugal", r"greece", r"singapore",
               r"malaysia", r"indonesia", r"thailand", r"vietnam", r"china", r"hong kong", r"japan", r"korea", r"new zealand",
               r"south africa", r"nigeria", r"kenya", r"ghana", r"morocco", r"algeria", r"tunisia", r"libya", r"brazil", r"mexico",
               r"argentina", r"chile", r"colombia", r"peru", r"guyana", r"trinidad", r"kazakhstan", r"azerbaijan", r"russia", r"ukraine"]
REGIONS = {
    "Canada": [r"ontario", r"british columbia", r"alberta", r"quebec", r"qu[ée]bec", r"manitoba", r"saskatchewan", r"nova scotia",
               r"new brunswick", r"newfoundland", r"prince edward island", r"yukon", r"nunavut", r"northwest territories"],
    "Australia": [r"new south wales", r"queensland", r"western australia", r"south australia", r"tasmania", r"northern territory",
                  r"australian capital territory", r"victoria(?!,?\s*(?:bc|british columbia|canada))"],
    "Other": [r"alabama", r"alaska", r"arizona", r"arkansas", r"california", r"colorado", r"connecticut", r"delaware", r"florida",
              r"georgia", r"hawaii", r"idaho", r"illinois", r"indiana", r"iowa", r"kansas", r"kentucky", r"louisiana", r"maine",
              r"maryland", r"massachusetts", r"michigan", r"minnesota", r"mississippi", r"missouri", r"montana", r"nebraska", r"nevada",
              r"new hampshire", r"new jersey", r"new mexico", r"new york", r"north carolina", r"north dakota", r"ohio", r"oklahoma",
              r"oregon", r"pennsylvania", r"rhode island", r"south carolina", r"south dakota", r"tennessee", r"texas", r"utah",
              r"vermont", r"virginia", r"washington", r"west virginia", r"wisconsin", r"wyoming"],
}
CA_CODES = {"ON", "BC", "AB", "QC", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NU", "NT"}
AU_CODES = {"NSW", "QLD", "VIC", "TAS", "ACT", "WA"}
CITIES = {
    "Saudi Arabia": ["riyadh", "jeddah", "jiddah", "dammam", "khobar", "al khobar", "jubail", "yanbu", "neom", "tabuk", "makkah", "mecca",
                     "madinah", "medina", "qiddiya", "dhahran", "abha", "taif", "qassim", "buraidah", "hafar al batin", "ras tanura", "khafji",
                     "umluj", "alula", "al ula", "rabigh", "jazan", "jizan", "najran", "hail", "al kharj", "khamis mushait", "hofuf", "al ahsa",
                     "al hasa", "ras al khair", "diriyah", "sakaka", "al jouf", "arar", "qurayyat", "amaala", "red sea project", "sudair", "wa'ad al shamal"],
    "United Arab Emirates": ["dubai", "abu dhabi", "sharjah", "ajman", "al ain", "fujairah", "ras al khaimah", "umm al quwain", "ruwais", "jebel ali",
                             "mussafah", "musaffah", "khalifa city", "al dhafra", "das island", "habshan", "khor fakkan", "dibba", "kizad"],
    "Qatar": ["doha", "lusail", "al wakrah", "ras laffan", "mesaieed", "al khor", "dukhan", "al rayyan", "umm said"],
    "Kuwait": ["ahmadi", "jahra", "salmiya", "shuwaikh", "fahaheel", "mina abdullah", "shuaiba", "burgan", "kuwait city"],
    "Oman": ["muscat", "sohar", "salalah", "duqm", "nizwa", "sur", "ibri", "barka", "rusayl", "khasab", "fahud", "seeb"],
    "Bahrain": ["manama", "muharraq", "sitra", "isa town", "riffa"],
    "Australia": ["sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra", "gold coast", "darwin", "hobart", "townsville",
                  "geelong", "wollongong", "cairns", "toowoomba", "ballarat", "bendigo", "launceston", "mackay", "rockhampton", "sunshine coast"],
    "Canada": ["toronto", "ottawa", "mississauga", "vancouver", "calgary", "edmonton", "montreal", "montréal", "winnipeg", "regina",
               "saskatoon", "halifax", "brampton", "hamilton", "kitchener", "surrey", "burnaby", "markham", "vaughan", "oakville", "kelowna",
               "fredericton", "moncton", "st. john's", "north vancouver", "chestermere", "red deer", "lethbridge", "fort mcmurray"],
}
_W = lambda p: re.compile(r"(?<![a-z])(?:" + p + r")(?![a-z])", re.I)
_TARGET_RX = [(c, _W("|".join(v))) for c, v in TARGET_NAMES.items()]
_OTHER_RX = _W("|".join(OTHER_NAMES))
_REGION_RX = [(c, _W("|".join(v))) for c, v in REGIONS.items()]
_CITY_RX = [(c, _W("|".join(re.escape(x) for x in v))) for c, v in CITIES.items()]
_CITY_RX_MAP = dict(_CITY_RX)
ISO_TARGET = {"SA": "Saudi Arabia", "SAU": "Saudi Arabia", "KSA": "Saudi Arabia", "AE": "United Arab Emirates", "ARE": "United Arab Emirates",
              "QA": "Qatar", "QAT": "Qatar", "KW": "Kuwait", "KWT": "Kuwait", "OM": "Oman", "OMN": "Oman", "BH": "Bahrain", "BHR": "Bahrain",
              "CA": "Canada", "CAN": "Canada", "AU": "Australia", "AUS": "Australia"}


VAGUE = re.compile(r"(?<![a-z])(gcc|middle east|mena|gulf region|gulf|remote|hybrid|various|multiple|several|global|worldwide|"
                   r"anywhere|\d+\s+locations?|not specified)(?![a-z])", re.I)
# Two-letter codes used both as a country/state code abroad and by a target country: decided by the city, else unknown.
AMBIGUOUS = {"CA": ("Canada", "California"), "WA": ("Australia", "Washington"), "NT": ("Australia|Canada", "-"), "SA": ("Saudi Arabia|Australia", "-")}


def guess_country(location: str) -> str:
    """Target country, 'Other' for a place clearly outside the targets, or '' if unknown."""
    loc = (location or "").strip()
    if not loc:
        return ""
    for c, rx in _TARGET_RX:
        if rx.search(loc):
            return c
    last_code = loc.split(",")[-1].strip().upper() if "," in loc else ""
    if last_code in CA_CODES - set(AMBIGUOUS):
        return "Canada"                                 # "Calgary, AB" - before other names ("Jordan Springs, NSW")
    if last_code in AU_CODES - set(AMBIGUOUS):
        return "Australia"
    for c, rx in _REGION_RX:
        if c != "Other" and rx.search(loc):
            return c                                    # "Sydney, New South Wales" before "Wales"
    if _OTHER_RX.search(loc):
        return "Other"
    for c, rx in _REGION_RX:
        if c == "Other" and rx.search(loc):
            return c
    city = next((c for c, rx in _CITY_RX if rx.search(loc)), "")
    loc = re.sub(r"(,\s*[^,]*\d{3,}[^,]*)+$", "", loc).strip()          # a postal code at the end ("Jeddah, SA, 21577")
    if not city and VAGUE.search(loc):
        return ""                                        # "GCC", "Remote", "3 Locations": unknown, not foreign
    if re.fullmatch(r"[A-Z]{2,3}", loc):                # the whole location is a country code ("UZ", "SAU")
        return ISO_TARGET.get(loc, "Other") if loc not in AMBIGUOUS else (city or "")
    # country codes: last comma token ("Muscat, OM", "Riyadh, SAU") or a leading "SA.Riyadh" / "AE - Dubai" / "AEDubai"
    last = loc.split(",")[-1].strip().upper() if "," in loc else ""
    coded = re.match(r"^(CA|AU)\s*[.-]\s*([A-Z]{2,3})\s*[.-]", loc)     # "CA.ON.Pickering.1340 ..." (country.province.city)
    if coded and coded.group(2) in (CA_CODES if coded.group(1) == "CA" else AU_CODES):
        return "Canada" if coded.group(1) == "CA" else "Australia"
    lead = re.match(r"^([A-Z]{2,3})\s*(?:\.|-+|\s-)\s*\S", loc) or re.match(r"^([A-Z]{2})(?=[A-Z][a-z])", loc)
    for code in (last, lead.group(1) if lead else ""):
        if code in AMBIGUOUS:
            if city:
                return city                              # "Adelaide, SA" = Australia, "Riyadh, SA" = Saudi Arabia
            if code == "SA" and lead:
                return "Saudi Arabia"                    # "SA.Riyadh" / "SA - Jeddah" style codes are Saudi
            return ""                                    # "El Segundo, CA", "Seattle, WA": not ours to guess
        if code in ISO_TARGET:
            return ISO_TARGET[code]
    for code in re.findall(r",\s*([A-Z]{2,3})\b", loc):
        if code in AMBIGUOUS:
            return city or ""
        if code in CA_CODES:
            return "Canada"
        if code in AU_CODES:
            return "Australia"
        if len(code) == 2:
            return city or "Other"                      # a US-style state code
    return city


def years_experience(profile: dict) -> int:
    """Whole years since candidate.experience_start (YYYY-MM); falls back to the fixed number."""
    c = profile["candidate"]
    try:
        from datetime import date
        y, m = (int(x) for x in str(c.get("experience_start", "")).split("-")[:2])
        t = date.today()
        return max(0, (t.year - y) * 12 + (t.month - m)) // 12
    except Exception:
        return int(c.get("years_experience", 0))


def fill_in(text, profile: dict, years: int | None = None):
    """Screening answers may say {years}, {notice} and {location}: filled from your details, so they never go stale."""
    if not isinstance(text, str):
        return text
    c = profile.get("candidate") or {}
    return (text.replace("{years}", str(years_experience(profile) if years is None else years))
                .replace("{notice}", str(c.get("notice_period") or "30 days"))
                .replace("{location}", str(c.get("location") or "Bahrain")))


def countries(profile: dict, flag: str = "search") -> list[str]:
    """Countries whose config has flag=True (config is {name: {weight, search, apply}})."""
    out = []
    for name, cfg in profile["search"]["countries"].items():
        if name.startswith("_"):
            continue
        if isinstance(cfg, dict) and cfg.get(flag, True):
            out.append(name)
        elif not isinstance(cfg, dict):
            out.append(name)
    return out


def country_weight(profile: dict, name: str):
    cfg = profile["search"]["countries"].get(name)
    if cfg is None:
        return None
    return cfg.get("weight", 1.0) if isinstance(cfg, dict) else float(cfg)


# ------------------------------------------------------------------ the same job seen on two sites
_CO_SUFFIX = re.compile(r"\b(corporation|corp|incorporated|inc|llc|l l c|ltd|limited|co|company|group|wll|w l l|pty|plc|pjsc|psc|llp|"
                        r"gmbh|holding|holdings|international|global|in the middle east|middle east|in canada|canada|australia|uae|ksa|"
                        r"saudi arabia|qatar|kuwait|oman)\b")


def norm_company(c: str) -> str:
    c = re.sub(r"[^\w\s&]", " ", (c or "").lower())
    return re.sub(r"\s+", " ", _CO_SUFFIX.sub(" ", c)).strip()


def city_of(loc: str) -> str:
    """The first place name of a location, loosely: 'Brisbane QLD' / 'Brisbane, Queensland, Australia' -> 'brisbane'."""
    loc = re.sub(r"^[A-Z]{2,3}\s*(?:\.|-+)\s*", "", (loc or "").strip())
    for c, rx in _CITY_RX:
        m = rx.search(loc)
        if m:
            return m.group(0).lower()
    first = re.split(r"[,(/|-]", loc)[0].strip().lower()
    first = re.sub(r"\b(nsw|qld|vic|wa|sa|tas|act|nt|on|bc|ab|qc)\b", "", first).strip()
    if not first or first in ("not specified", "remote", "hybrid") or VAGUE.search(first) or guess_country(first) not in ("",):
        return ""
    return first.split()[0] if first else ""


def same_job_key(j: dict) -> str:
    """Title + company + country (+ city when known), written loosely ('Parsons Corporation' = 'Parsons'): the same posting
    on LinkedIn, SEEK and the employer's own site. Empty when the company is unknown (then only the link identifies a job)."""
    co = norm_company(j.get("company", ""))
    if not co or co == "unknown" or co == "private advertiser":
        return ""
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s/&+]", " ", (j.get("title") or "").lower())).strip()
    return f"tcc:{t}|{co}|{j.get('country') or ''}|{city_of(j.get('location') or '')}"


def is_known(j: dict) -> bool:
    k = same_job_key(j)
    return dedupe_key(j) in KNOWN or bool(k and k in KNOWN)


# Incremental searching (set by main): only ads posted in the last WINDOW_DAYS days are asked for (None = each source's
# own default, i.e. a full search), and KNOWN holds the jobs already in your list so paging can stop when nothing is new.
WINDOW_DAYS: int | None = None
SOURCE_WINDOWS: dict[str, int | None] = {}      # per source: a source that failed recently gets a longer look-back
KNOWN: set[str] = set()


def window(default: int, source: str = "") -> int:
    w = SOURCE_WINDOWS.get(source, WINDOW_DAYS) if source else WINDOW_DAYS
    return max(1, min(default, w)) if w else default


ID_PARAMS = re.compile(r"(?:^|[?&])(jk|vjk|jobid|job_id|gh_jid)=([^&#]+)", re.I)


def dedupe_key(j: dict) -> str:
    m = ID_PARAMS.search(j["url"])
    if m and "indeed." in j["url"].lower():
        return "indeed:" + m.group(2).lower()        # Indeed: the job id is in ?jk=, the same on every country site
    if m:
        return j["url"].lower().split("?")[0].rstrip("/") + f"?{m.group(1).lower()}={m.group(2).lower()}"
    u = j["url"].lower().split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"linkedin\.com/jobs/view/(?:[^/]*-)?(\d{6,})", u)
    if m:
        return "linkedin:" + m.group(1)             # same job on sa./ae./www. subdomains
    if u.startswith("http"):
        return u
    return "|".join([j["title"], j["company"], j["location"]]).lower()
