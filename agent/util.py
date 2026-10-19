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


def http(url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 25, retries: int = 2) -> tuple[int, str]:
    """GET (or POST when data given). Returns (status, body). Never raises."""
    h = dict(BASE_HEADERS)
    if headers:
        h.update(headers)
    for attempt in range(retries + 1):
        try:
            req = Request(url, data=data, headers=h, method="POST" if data else "GET")
            with urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
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
        "url": (url or "").strip(),
        "description": clean(description),
        "source": source,
        "posted": posted or "",
    }


COUNTRY_HINTS = {
    "Saudi Arabia": ["saudi", "riyadh", "jeddah", "dammam", "khobar", "jubail", "yanbu", "neom", "tabuk", "mecca", "makkah", "madinah", "medina", "qiddiya", "ksa", "dhahran", "abha", "hail", "taif", "al khobar"],
    "United Arab Emirates": ["uae", "united arab emirates", "dubai", "abu dhabi", "sharjah", "ajman", "al ain", "fujairah", "ras al khaimah", "umm al quwain"],
    "Qatar": ["qatar", "doha", "lusail", "al wakrah"],
    "Kuwait": ["kuwait"],
    "Oman": ["oman", "muscat", "sohar", "salalah", "duqm"],
    "Bahrain": ["bahrain", "manama", "muharraq"],
    "Canada": ["canada", "ontario", "toronto", "ottawa", "mississauga", "british columbia", "vancouver", "alberta", "calgary",
               "edmonton", "quebec", "montreal", "montréal", "manitoba", "winnipeg", "saskatchewan", "regina", "saskatoon",
               "nova scotia", "halifax", "new brunswick", "newfoundland", ", on", ", bc", ", ab", ", qc", ", mb", ", sk", ", ns", ", nb"],
}


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


def guess_country(location: str) -> str:
    loc = (location or "").lower()
    for country, hints in COUNTRY_HINTS.items():
        if any(h in loc for h in hints):
            return country
    return ""


def dedupe_key(j: dict) -> str:
    u = j["url"].lower().split("?")[0].rstrip("/")
    if u.startswith("http"):
        return u
    return "|".join([j["title"], j["company"], j["location"]]).lower()
