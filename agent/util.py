"""Shared helpers: HTTP, text cleaning, job record shape."""
from __future__ import annotations
import html, json, re, sys, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


MAX_BYTES = 5 * 1024 * 1024


def safe_url(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname)


def http(url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 25, retries: int = 2) -> tuple[int, str]:
    """GET (or POST when data given). Returns (status, body). Never raises. Only http(s); bodies capped at 5 MB."""
    if not safe_url(url):
        log(f"  refused non-web link {str(url)[:60]}")
        return 0, ""
    h = dict(BASE_HEADERS)
    if headers:
        h.update(headers)
    for attempt in range(retries + 1):
        try:
            req = Request(url, data=data, headers=h, method="POST" if data else "GET")
            with urlopen(req, timeout=timeout) as r:
                raw = r.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    log(f"  response too large, skipped {url[:80]}")
                    return 0, ""
                return r.status, raw.decode("utf-8", errors="replace")
        except HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            return e.code, ""
        except (URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(2)
                continue
            log(f"  network error {url[:80]}: {e}")
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


def clean(value) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def job(title, company, location, url, description="", source="", country="", posted=""):
    return {
        "title": clean(title),
        "company": clean(company) or "Unknown",
        "location": clean(location) or "Not specified",
        "country": country or guess_country(clean(location)),
        "url": (url or "").strip() if safe_url(url) else "",
        "description": clean(description),
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
                  r"australian capital territory"],
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
                     "madinah", "qiddiya", "dhahran", "abha", "taif", "qassim", "buraidah", "hafar al batin", "ras tanura", "khafji", "umluj", "alula"],
    "United Arab Emirates": ["dubai", "abu dhabi", "sharjah", "ajman", "al ain", "fujairah", "ras al khaimah", "umm al quwain", "ruwais", "jebel ali"],
    "Qatar": ["doha", "lusail", "al wakrah", "ras laffan", "mesaieed"],
    "Kuwait": ["ahmadi", "jahra", "salmiya"],
    "Oman": ["muscat", "sohar", "salalah", "duqm", "nizwa", "sur"],
    "Bahrain": ["manama", "muharraq", "sitra", "isa town", "riffa"],
    "Australia": ["sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra", "gold coast", "darwin", "hobart", "townsville",
                  "geelong", "wollongong", "cairns", "toowoomba", "ballarat", "bendigo", "launceston", "mackay", "rockhampton", "sunshine coast"],
    "Canada": ["toronto", "ottawa", "mississauga", "vancouver", "calgary", "edmonton", "montreal", "montréal", "winnipeg", "regina",
               "saskatoon", "halifax", "brampton", "hamilton", "kitchener", "surrey", "burnaby", "markham", "vaughan", "oakville", "kelowna",
               "victoria", "fredericton", "moncton", "st. john's"],
}
_W = lambda p: re.compile(r"(?<![a-z])(?:" + p + r")(?![a-z])", re.I)
_TARGET_RX = [(c, _W("|".join(v))) for c, v in TARGET_NAMES.items()]
_OTHER_RX = _W("|".join(OTHER_NAMES))
_REGION_RX = [(c, _W("|".join(v))) for c, v in REGIONS.items()]
_CITY_RX = [(c, _W("|".join(re.escape(x) for x in v))) for c, v in CITIES.items()]


def guess_country(location: str) -> str:
    """Target country, 'Other' for a place clearly outside the targets, or '' if unknown."""
    loc = (location or "").strip()
    if not loc:
        return ""
    for c, rx in _TARGET_RX:
        if rx.search(loc):
            return c
    if _OTHER_RX.search(loc):
        return "Other"
    for c, rx in _REGION_RX:
        if rx.search(loc):
            return c
    for code in re.findall(r",\s*([A-Z]{2,3})\b", loc):
        if code in CA_CODES:
            return "Canada"
        if code in AU_CODES:
            return "Australia"
        if len(code) == 2:
            return "Other"                      # a US-style state code
    for c, rx in _CITY_RX:
        if rx.search(loc):
            return c
    return ""


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


def dedupe_key(j: dict) -> str:
    u = j["url"].lower().split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"linkedin\.com/jobs/view/(?:[^/]*-)?(\d{6,})", u)
    if m:
        return "linkedin:" + m.group(1)             # same job on sa./ae./www. subdomains
    if u.startswith("http"):
        return u
    return "|".join([j["title"], j["company"], j["location"]]).lower()
