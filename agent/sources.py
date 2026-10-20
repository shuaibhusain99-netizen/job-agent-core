"""Job fetchers. Every fetcher returns a list of job dicts (see util.job) and never raises."""
from __future__ import annotations
import html, json, os, re, time
from urllib.parse import quote, quote_plus, urljoin
from xml.etree import ElementTree as ET

from pathlib import Path
from .util import http, http_json, job, clean, log, guess_country, countries, window, dedupe_key
from . import util
from . import sitebrowse


# ---------------------------------------------------------------- Jooble (free API key)
JOOBLE_SITES = {"Saudi Arabia": "sa", "United Arab Emirates": "ae", "Qatar": "qa", "Kuwait": "kw", "Oman": "om",
                "Bahrain": "bh", "Canada": "ca", "Australia": "au"}


KEYS_PATH = Path(__file__).resolve().parents[1] / "keys.json"


def all_queries(profile: dict) -> list[str]:
    """civil + other + oil & gas search phrases, de-duplicated, order kept."""
    sp = profile["search"]
    out, seen = [], set()
    for q in sp["queries"] + sp.get("queries_other", []) + sp.get("queries_oilgas", []) + sp.get("queries_entry", []):
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def load_keys() -> dict:
    try:
        return json.loads(KEYS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fetch_jooble(cfg: dict, profile: dict) -> list[dict]:
    """Jooble has one API key per country site (sa.jooble.org, ae.jooble.org ...). keys.json → jooble.<cc>.
    The global jooble.org key (keys.json → jooble.us, or env JOOBLE_API_KEY) is used for countries without their own key."""
    if not cfg.get("enabled"):
        return []
    keys = (load_keys().get("jooble") or {})
    global_key = (keys.get("us") or os.getenv("JOOBLE_API_KEY", "")).strip()
    out = []
    budget = cfg.get("max_requests_per_run", 60)
    used = 0
    for country in countries(profile):
        cc = JOOBLE_SITES.get(country, "")
        key = (keys.get(cc) or "").strip()
        host = f"{cc}.jooble.org" if key else "jooble.org"
        if not key:
            key = global_key
            if not key or (cfg.get("global_key_countries") and country not in cfg["global_key_countries"]):
                continue
        for q in cfg.get("queries") or profile["search"]["queries"]:
            for page in range(1, cfg.get("pages_per_query", 1) + 1):
                if used >= budget:
                    break
                used += 1
                status, data = http_json(f"https://{host}/api/{key}",
                                         {"keywords": q, "location": "" if host != "jooble.org" else country, "page": page})
                if status != 200 or not data:
                    break
                rows = data.get("jobs", [])
                for r in rows:
                    out.append(job(r.get("title"), r.get("company"), r.get("location"), r.get("link"),
                                   r.get("snippet", ""), "Jooble", country, (r.get("updated") or "")[:10]))
                if len(rows) < 20:
                    break
                time.sleep(0.5)
    log(f"Jooble: {len(out)} rows ({used} requests)")
    return out


# ---------------------------------------------------------------- Google Alerts RSS (free)
def fetch_rss(cfg: dict, profile: dict) -> list[dict]:
    out = []
    for url in cfg.get("feeds", []) if cfg.get("enabled") else []:
        status, body = http(url)
        if status != 200 or not body:
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall(".//a:entry", ns) + root.findall(".//item"):
            title = (e.findtext("a:title", default="", namespaces=ns) or e.findtext("title") or "")
            link_el = e.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else (e.findtext("link") or "")
            # Google Alerts wraps the real URL: https://www.google.com/url?...&url=<real>
            m = re.search(r"[?&]url=([^&]+)", link or "")
            if m:
                from urllib.parse import unquote
                link = unquote(m.group(1))
            summary = e.findtext("a:content", default="", namespaces=ns) or e.findtext("description") or ""
            out.append(job(title, "", "", link, summary, "Google Alerts"))
    log(f"Google Alerts RSS: {len(out)} rows")
    return out


# ---------------------------------------------------------------- GulfTalent (best effort HTML)
def fetch_gulftalent(cfg: dict, profile: dict) -> list[dict]:
    if not cfg.get("enabled"):
        return []
    out, blocked = [], False
    for country, slug in cfg["country_slugs"].items():
        if country not in countries(profile) or blocked:
            continue
        for t in cfg["title_slugs"]:
            status, body = http(f"https://www.gulftalent.com/{slug}/jobs/title/{t}")
            if status in (403, 429, 503):
                blocked = True
                break
            if status != 200:
                continue
            for m in re.finditer(r'<a[^>]+href="(/[a-z-]+/jobs/[a-z0-9-]+-\d+)"[^>]*>\s*([^<]{3,120})</a>', body):
                url = "https://www.gulftalent.com" + m.group(1)
                title = clean(m.group(2))
                if not title or title.lower().startswith("jobs in"):
                    continue
                # company / location sit in the surrounding row; grab a window of text
                window = clean(body[m.end(): m.end() + 600])
                parts = [p.strip() for p in re.split(r"\s{2,}|\t", window) if p.strip()]
                company = parts[0] if parts else ""
                out.append(job(title, company, country, url, "", "GulfTalent", country))
            time.sleep(0.7)
    log(f"GulfTalent: {'blocked (403) - skipped' if blocked else f'{len(out)} rows'}")
    return out


# ---------------------------------------------------------------- LinkedIn guest listing (best effort)
def _or(qs: list[str]) -> str:
    return " OR ".join(f'"{q}"' if " " in q else q for q in qs)


def linkedin_groups(profile: dict, cfg: dict) -> list[tuple[str, int]]:
    """Your search phrases joined with OR, a few searches instead of ~40: each job comes back once instead of once per
    phrase, so far fewer requests reach much deeper into the results. (keywords, page limit)"""
    sp = profile["search"]
    seen, groups = set(), []

    def add(qs, pages):
        qs = [q for q in qs if q.lower() not in seen and not seen.add(q.lower())]
        for i in range(0, len(qs), 10):                     # keep each search a sensible length
            groups.append((_or(qs[i:i + 10]), pages))
    add(sp["queries"] + sp.get("queries_entry", []), int(cfg.get("pages_civil", 25)))
    add(sp.get("queries_other", []), int(cfg.get("pages_other", 10)))
    add(sp.get("queries_oilgas", []), int(cfg.get("pages_other", 10)))
    return groups


def fetch_linkedin(cfg: dict, profile: dict) -> list[dict]:
    if not cfg.get("enabled"):
        return []
    out, blocked_countries = [], 0
    days = window(int(cfg.get("days", 14)))
    tpr = f"r{days * 86400}"
    groups = linkedin_groups(profile, cfg)
    requests = 0
    for country in countries(profile):
        country_hits, challenged, seen = 0, 0, set()
        for kw, max_pages in groups:
            start, stale = 0, 0
            for page in range(max_pages):
                url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
                       f"keywords={quote_plus(kw)}&location={quote_plus(country)}&f_TPR={tpr}&start={start}")
                status, body = http(url)
                requests += 1
                if status == 200 and "base-card" not in body:
                    break                                   # no (more) results
                if status != 200:
                    challenged += 1
                    time.sleep(3)
                    break
                cards = re.findall(r"<li>[\s\S]*?</li>", body)
                new = unknown = 0
                for card in cards:
                    t = re.search(r'base-search-card__title">\s*([^<]+)', card)
                    c = re.search(r'class="hidden-nested-link"[^>]*>\s*([^<]+)', card)
                    l = re.search(r'job-search-card__location">\s*([^<]+)', card)
                    u = re.search(r'href="(https://[^"?]+)', card)
                    d = re.search(r'datetime="([^"]+)"', card)
                    if not (t and u):
                        continue
                    loc = l.group(1) if l else country
                    j = job(t.group(1), c.group(1) if c else "", loc, u.group(1), "", "LinkedIn",
                            guess_country(clean(loc)) or country, d.group(1)[:10] if d else "")
                    k = dedupe_key(j)
                    if k in seen:
                        continue
                    seen.add(k)
                    new += 1
                    unknown += k not in util.KNOWN
                    out.append(j)
                    country_hits += 1
                time.sleep(1.0)
                if not cards or not new:
                    break                                   # the results started repeating
                stale = stale + 1 if not unknown else 0
                if stale >= 2:
                    break                                   # two pages of jobs you already have: the rest is older news
                start += len(cards)
            if challenged >= 3:
                break
        log(f"LinkedIn {country}: {country_hits} jobs" + (" (stopped: LinkedIn is limiting requests)" if challenged >= 3 else ""))
        blocked_countries = blocked_countries + 1 if challenged >= 3 else 0
        if blocked_countries >= 2:
            log("LinkedIn: limited in two countries in a row - trying again next run")
            break
    log(f"LinkedIn: {len(out)} jobs from {requests} requests (posted in the last {days} days)")
    return out


def enrich_linkedin(j: dict) -> None:
    m = re.search(r"-(\d{6,})/?$", j["url"].split("?")[0])
    if not m:
        return
    status, body = http(f"https://www.linkedin.com/jobs/view/{m.group(1)}")
    if status != 200:
        return
    d = re.search(r'show-more-less-html__markup[^>]*>([\s\S]*?)</div>', body)
    if d:
        j["description"] = clean(d.group(1))[:6000]
    for label, val in re.findall(r'description__job-criteria-subheader">\s*([^<]+)</h3>\s*<span[^>]*>\s*([^<]+)', body):
        if "Seniority" in label:
            j["description"] = f"Seniority level: {clean(val)}. " + j["description"]
    if j["company"] == "Unknown":
        t = re.search(r"<title>([^|<]+?)\s+hiring\s", body)
        if t:
            j["company"] = clean(t.group(1))
    time.sleep(0.8)


# ---------------------------------------------------------------- SEEK (Australia, public API)
def fetch_seek(cfg: dict, profile: dict) -> list[dict]:
    if not cfg.get("enabled") or "Australia" not in countries(profile):
        return []
    out, seen = [], set()
    for q in cfg.get("queries", ["civil engineer"]) + profile["search"].get("queries_other", []) + profile["search"].get("queries_oilgas", []):
        for page in range(1, int(cfg.get("pages", 2)) + 1):
            url = ("https://www.seek.com.au/api/jobsearch/v5/search?siteKey=AU-Main&sourcesystem=houston"
                   f"&keywords={quote_plus(q)}&page={page}&pageSize=50&locale=en-AU&sortmode=ListedDate&daterange={window(31)}")
            status, data = http_json(url)
            if status != 200 or not data:
                log(f"SEEK: HTTP {status} on '{q}' p{page}")
                break
            rows = data.get("data", [])
            for r in rows:
                jid = str(r.get("id"))
                if jid in seen:
                    continue
                seen.add(jid)
                loc = ", ".join(x.get("label", "") for x in (r.get("locations") or [])) or "Australia"
                desc = " ".join([r.get("teaser", "")] + (r.get("bulletPoints") or []) + [r.get("salaryLabel", "") or ""])
                out.append(job(r.get("title"), (r.get("advertiser") or {}).get("description") or r.get("companyName"),
                               loc, f"https://www.seek.com.au/job/{jid}", desc, "SEEK", "Australia", (r.get("listingDate") or "")[:10]))
            if len(rows) < 50:
                break
            time.sleep(0.6)
    log(f"SEEK: {len(out)} rows")
    return out


# ---------------------------------------------------------------- Workday (employer ATS, reliable)
WD_CODES = {"AE": "United Arab Emirates", "SA": "Saudi Arabia", "KSA": "Saudi Arabia", "QA": "Qatar", "KW": "Kuwait", "OM": "Oman",
            "BH": "Bahrain", "CA": "Canada", "AU": "Australia"}
WD_COUNTRY_IDS = {"Saudi Arabia": "50423b5190ad49bb89e94cd58dfaad69", "United Arab Emirates": "7b4fa1f369bd4604ba3692682fcbe345",
                  "Qatar": "f5ff182c7fab406bbb110cc1f7e99064", "Oman": "e8424552931249ba9c6c1d7706cdc01d",
                  "Kuwait": "eb2e6ec0f73c4b0e94c68396631332d6", "Canada": "a30a87ed25634629aa6c3958aa2b91ea"}
WD_QUERIES = ["civil", "site engineer", "project engineer", "QA/QC", "planning engineer", "quantity surveyor", "HSE",
              "document controller", "field engineer", "construction"]


def _wd_country(path: str, loc: str) -> str:
    """Workday hides the location in the URL: /job/AE---Abu-Dhabi/..., /job/Riyadh-Saudi-Arabia/..., /job/GB-NEWCASTLE/..."""
    c = guess_country(loc)
    if c:
        return c
    m = re.match(r"/job/([^/]+)/", path or "")
    if not m:
        return ""
    seg = m.group(1)
    code = re.match(r"([A-Z]{2,3})(?:-|$)", seg)
    if code:
        return WD_CODES.get(code.group(1), "Other")
    return guess_country(seg.replace("-", " "))


def _wd_posted(text: str) -> str:
    from datetime import date, timedelta
    t = (text or "").lower()
    if "today" in t:
        d = 0
    elif "yesterday" in t:
        d = 1
    else:
        m = re.search(r"(\d+)\+?\s*days?", t)
        if not m:
            return ""
        d = int(m.group(1)) + (1 if "+" in t else 0)
    return (date.today() - timedelta(days=d)).isoformat()


def fetch_workday(cfg: dict, profile: dict) -> list[dict]:
    out = []
    targets = set(profile["target_titles"])
    for c in profile.get("categories", {}).values():
        if isinstance(c, dict):
            targets |= set(c.get("target_titles", []))
    pages = int(cfg.get("pages", 5))
    for b in cfg.get("boards", []):
        base = f"https://{b['tenant']}.{b['host']}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{b['tenant']}/{b['site']}/jobs"
        seen, mine, failed = set(), [], False
        for q in cfg.get("queries") or WD_QUERIES:
            for page in range(pages):
                facets = {b["country_param"]: [WD_COUNTRY_IDS[c] for c in countries(profile) if c in WD_COUNTRY_IDS]} if b.get("country_param") else {}
                status, data = http_json(api, {"appliedFacets": facets, "limit": 20, "offset": page * 20, "searchText": q})
                if status != 200 or not data:
                    if status:
                        log(f"Workday {b['company']}: HTTP {status}")
                    failed = status in (401, 403, 404)
                    break
                rows = data.get("jobPostings", [])
                for p in rows:
                    path = p.get("externalPath", "")
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    loc = p.get("locationsText") or ""
                    country = _wd_country(path, loc)
                    if country and country not in countries(profile):
                        continue
                    mine.append(job(p.get("title"), b["company"], loc or country, f"{base}/{b['site']}{path}", "",
                                    "Career site", country, _wd_posted(p.get("postedOn", ""))))
                time.sleep(0.4)
                if len(rows) < 20 or (page + 1) * 20 >= int(data.get("total") or 0):
                    break
            if failed:
                break
        # still unknown after the URL check: ask the detail endpoint, for titles worth it
        resolved = 0
        for j in mine:
            if j["country"] or resolved >= 40:
                continue
            t = j["title"].lower()
            if any(w in t for w in targets) and not re.search(r"\b(senior|principal|lead|manager|director|national)\b", t):
                enrich_workday(j)
                resolved += 1
                time.sleep(0.3)
        out += [j for j in mine if not (j["country"] and j["country"] not in countries(profile))]
        log(f"Workday {b['company']}: {len(seen)} listings, {len(mine)} in target countries or unknown, {resolved} detail lookups")
    return out


def enrich_workday(j: dict) -> None:
    """Pull the full description for a Workday posting (used for top matches only)."""
    m = re.match(r"(https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com)/([^/]+)(/.+)", j["url"])
    if not m:
        return
    base, tenant, _, site, path = m.groups()
    status, data = http_json(f"{base}/wday/cxs/{tenant}/{site}{path}")
    if status == 200 and data:
        info = data.get("jobPostingInfo", {})
        j["description"] = clean(info.get("jobDescription", ""))
        j["location"] = clean(info.get("location", j["location"]))
        j["country"] = j["country"] or guess_country(j["location"])
        if info.get("startDate") and not j.get("posted"):
            j["posted"] = str(info["startDate"])[:10]


# ---------------------------------------------------------------- SuccessFactors (employer ATS)
def fetch_successfactors(cfg: dict, profile: dict) -> list[dict]:
    out = []
    for b in cfg.get("boards", []):
        seen, more = set(), {}
        for q, startrow in [(q, r) for q in ["civil", "site engineer", "project engineer", "QA/QC", "infrastructure", "HSE",
                                                  "quantity surveyor", "planning", "storekeeper"] for r in (0, 25)]:
            root = f"{b['base']}/{b['slug']}" if b.get("slug") else b["base"]
            url = f"{root}/search/?q={quote_plus(q)}&locationsearch=&startrow={startrow}"
            if startrow and not more.get(q):
                continue
            status, body = http(url)
            if status != 200:
                log(f"SuccessFactors {b['company']}: HTTP {status}")
                break
            more[q] = f"startrow={startrow + 25}" in body or bool(re.search(r'title="Page 2"|class="paginationItemLast"', body))
            for m in re.finditer(r'<tr class="data-row">([\s\S]*?)</tr>', body):
                row = m.group(1)
                a = re.search(r'href="([^"]+)"[^>]*class="jobTitle-link"[^>]*>([^<]+)</a>', row) or \
                    re.search(r'class="jobTitle-link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', row)
                if not a:
                    continue
                href, title = a.groups()
                if href in seen:
                    continue
                seen.add(href)
                loc_m = re.search(r'<span class="jobLocation">\s*([^<]+)', row)
                loc = clean(loc_m.group(1)) if loc_m else ""
                country = guess_country(loc) or b.get("country", "")      # e.g. Red Sea Global lists "Not specified" (all KSA)
                if country and country not in countries(profile):
                    continue
                out.append(job(title, b["company"], loc, urljoin(b["base"], href), "", "Career site", country))
            time.sleep(0.5)
        log(f"SuccessFactors {b['company']}: {len(seen)} rows")
    return out


def enrich_successfactors(j: dict) -> None:
    status, body = http(j["url"])
    if status == 200:
        m = re.search(r'<span class="jobdescription">([\s\S]*?)</span>\s*</div>', body) or \
            re.search(r'class="jobDisplay[\s\S]*?<div[^>]*>([\s\S]{200,20000}?)</div>\s*</div>', body)
        if m:
            j["description"] = clean(m.group(1))[:6000]


# ---------------------------------------------------------------- Workable
def fetch_workable(cfg: dict, profile: dict) -> list[dict]:
    out = []
    for a in cfg.get("accounts", []):
        results, token, status = [], None, 0
        for _ in range(4):
            body = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
            if token:
                body["token"] = token
            status, data = http_json(f"https://apply.workable.com/api/v3/accounts/{a['account']}/jobs", body)
            if status != 200 or not data:
                break
            results += data.get("results", [])
            token = data.get("nextPage")
            if not token:
                break
            time.sleep(2)
        if not results:
            log(f"Workable {a['company']}: HTTP {status}")
            time.sleep(2)
            continue
        n = 0
        for r in results:
            loc = r.get("location") or {}
            loc_txt = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
            country = guess_country(loc_txt)
            if country and country not in countries(profile):
                continue
            url = f"https://apply.workable.com/{a['account']}/j/{r.get('shortcode')}/"
            out.append(job(r.get("title"), a["company"], loc_txt, url, "", "Career site", country, (r.get("published") or "")[:10]))
            n += 1
        log(f"Workable {a['company']}: {n} rows")
        time.sleep(2)
    return out


def enrich_workable(j: dict) -> None:
    m = re.search(r"apply\.workable\.com/([^/]+)/j/([A-Z0-9]+)", j["url"])
    if not m:
        return
    status, data = http_json(f"https://apply.workable.com/api/v2/accounts/{m.group(1)}/jobs/{m.group(2)}")
    if status == 200 and data:
        j["description"] = clean((data.get("description") or "") + " " + (data.get("requirements") or ""))[:6000]


# ---------------------------------------------------------------- Greenhouse / Lever / SmartRecruiters
def fetch_greenhouse(cfg: dict, profile: dict) -> list[dict]:
    out = []
    for b in cfg.get("boards", []):
        status, data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{quote(b['token'])}/jobs?content=true")
        if status != 200 or not data:
            continue
        for it in data.get("jobs", []):
            loc = (it.get("location") or {}).get("name", "")
            out.append(job(it.get("title"), b.get("company", b["token"]), loc, it.get("absolute_url"), it.get("content", ""), "Greenhouse"))
    return out


def fetch_lever(cfg: dict, profile: dict) -> list[dict]:
    out = []
    for b in cfg.get("boards", []):
        host = "api.eu.lever.co" if b.get("region") == "eu" else "api.lever.co"
        status, data = http_json(f"https://{host}/v0/postings/{quote(b['site'])}?mode=json")
        if status != 200 or not isinstance(data, list):
            continue
        for it in data:
            cat = it.get("categories") or {}
            text = " ".join([it.get("descriptionPlain") or ""] +
                            [f"{l.get('text', '')}: {clean(l.get('content', ''))}" for l in it.get("lists") or []] +
                            [it.get("additionalPlain") or ""])
            out.append(job(it.get("text"), b.get("company", b["site"]), cat.get("location", ""), it.get("hostedUrl"),
                           clean(text)[:6000], "Lever"))
    return out


SR_CC = {"Saudi Arabia": "sa", "United Arab Emirates": "ae", "Qatar": "qa", "Kuwait": "kw", "Oman": "om", "Canada": "ca", "Australia": "au"}
EMPLOYER_QUERIES = ["civil", "site engineer", "QA/QC", "planning engineer", "quantity surveyor", "HSE", "document controller",
                    "storekeeper", "technician", "graduate"]


def fetch_smartrecruiters(cfg: dict, profile: dict) -> list[dict]:
    """SmartRecruiters public API: every posting of the company in each country (a few pages of 100), not one search per
    phrase - the scoring decides what's relevant. (Egis, Masdar, SGS, Turner & Townsend, Ramboll ...)"""
    out = []
    for c in cfg.get("companies", []):
        cid, name = (c, c) if isinstance(c, str) else (c["id"], c.get("company", c["id"]))
        seen = set()
        for country in countries(profile):
            cc = SR_CC.get(country)
            if not cc:
                continue
            q = f"&q={quote_plus(cfg['queries'][0])}" if cfg.get("queries") else ""      # (tests)
            for offset in range(0, int(cfg.get("max_per_country", 400)), 100):
                status, data = http_json(f"https://api.smartrecruiters.com/v1/companies/{quote(cid)}/postings?limit=100&offset={offset}&country={cc}{q}")
                if status != 200 or not data:
                    break
                rows = data.get("content", [])
                for it in rows:
                    if it.get("id") in seen:
                        continue
                    seen.add(it.get("id"))
                    loc = it.get("location") or {}
                    loc_txt = ", ".join(x for x in [loc.get("city"), country] if x)
                    out.append(job(it.get("name"), name, loc_txt, f"https://jobs.smartrecruiters.com/{cid}/{it.get('id')}", "",
                                   "Career site", country, (it.get("releasedDate") or "")[:10]))
                if len(rows) < 100 or offset + 100 >= int(data.get("totalFound") or 0):
                    break
                time.sleep(0.3)
        log(f"SmartRecruiters {name}: {len(seen)} listings")
    return out


def _search_countries(profile: dict) -> list[str]:
    return [c for c in countries(profile) if c != "Bahrain"]


def fetch_phenom(cfg: dict, profile: dict) -> list[dict]:
    """Phenom career sites (ADNOC, Bechtel, Mace, Ma'aden): the JSON the site's own search page uses."""
    out = []
    for b in cfg.get("boards", []):
        base = b["base"].rstrip("/")                       # e.g. https://jobs.adnoc.ae/us/en
        m = re.match(r"(https://[^/]+)/([a-z]{2})/([a-z]{2})", base)
        if not m:
            continue
        host, cc, lang = m.groups()
        seen = set()
        for q in cfg.get("queries") or EMPLOYER_QUERIES:
            for page in range(int(cfg.get("pages", 2))):
                body = {"lang": f"{lang}_{cc}", "deviceType": "desktop", "country": cc, "pageName": "search-results", "ddoKey": "refineSearch",
                        "sortBy": "Most recent", "subsearch": "", "from": page * 50, "jobs": True, "counts": True, "all_fields": ["country", "city"],
                        "size": 50, "clearAll": False, "jdsource": "facets", "isSliderEnable": False, "pageId": "page20", "siteType": "external",
                        "keywords": q, "global": True, "selected_fields": {}, "locationData": {}}
                status, data = http_json(f"{host}/widgets", body)
                rs = (data or {}).get("refineSearch") or {}
                jobs_ = (rs.get("data") or {}).get("jobs") or []
                if status != 200 or not jobs_:
                    break
                for r in jobs_:
                    jid = str(r.get("jobId") or r.get("reqId") or "")
                    if not jid or jid in seen:
                        continue
                    seen.add(jid)
                    loc = r.get("location") or ", ".join(x for x in [r.get("city"), r.get("country")] if x)
                    country = guess_country(loc)
                    if country and country not in countries(profile):
                        continue
                    slug = re.sub(r"[^a-z0-9]+", "-", (r.get("title") or "").lower()).strip("-")
                    out.append(job(r.get("title"), b["company"], loc, f"{base}/job/{jid}/{slug}", r.get("descriptionTeaser") or "",
                                   "Career site", country, (r.get("postedDate") or "")[:10]))
                if (page + 1) * 50 >= int(rs.get("totalHits") or 0):
                    break
                time.sleep(0.6)
        log(f"Phenom {b['company']}: {len(seen)} listings")
    return out


def fetch_eightfold(cfg: dict, profile: dict) -> list[dict]:
    """Eightfold career sites (NEOM, Worley, Fluor). api 'v2' = /api/apply/v2/jobs, 'pcsx' = /api/pcsx/search."""
    from datetime import datetime, timezone
    out = []
    for b in cfg.get("boards", []):
        host, domain, api = b["host"], b["domain"], b.get("api", "v2")
        seen = set()
        for country in _search_countries(profile):
            for q in cfg.get("queries") or EMPLOYER_QUERIES[:6]:
                for page in range(int(cfg.get("pages", 2))):
                    if api == "pcsx":
                        url = (f"https://{host}/api/pcsx/search?domain={quote(domain)}&query={quote_plus(q)}&location={quote_plus(country)}"
                               f"&start={page * 10}&sort_by=timestamp")
                    else:
                        url = (f"https://{host}/api/apply/v2/jobs?domain={quote(domain)}&start={page * 10}&num=10&query={quote_plus(q)}"
                               f"&location={quote_plus(country)}&sort_by=relevance")
                    status, data = http_json(url)
                    if status != 200 or not data:
                        break
                    d = data.get("data", data)
                    rows = d.get("positions") or []
                    for r in rows:
                        pid = str(r.get("id") or r.get("atsJobId") or r.get("ats_job_id") or "")
                        if not pid or pid in seen:
                            continue
                        seen.add(pid)
                        locs = r.get("standardizedLocations") or r.get("locations") or [r.get("location") or ""]
                        loc = locs[0] if locs else ""
                        c = guess_country(loc)
                        if c and c not in countries(profile):
                            continue
                        link = r.get("canonicalPositionUrl") or r.get("positionUrl") or ""
                        if link.startswith("/"):
                            link = f"https://{host}{link}"
                        ts = r.get("postedTs") or r.get("t_create")
                        posted = datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat() if ts else ""
                        out.append(job(r.get("name"), b["company"], loc, link, r.get("job_description") or "", "Career site", c, posted))
                    if len(rows) < 10:
                        break
                    time.sleep(0.5)
        log(f"Eightfold {b['company']}: {len(seen)} listings")
    return out


def fetch_jobsyn(cfg: dict, profile: dict) -> list[dict]:
    """DirectEmployers .jobs sites (AECOM, Stantec, Jacobs): one search API, the x-origin header picks the employer.
    The listing already carries the full ad text."""
    out = []
    for b in cfg.get("boards", []):
        seen = set()
        for country in _search_countries(profile):
            for q in cfg.get("queries") or EMPLOYER_QUERIES[:6]:
                for page in range(1, int(cfg.get("pages", 2)) + 1):
                    url = f"https://prod-search-api.jobsyn.org/api/v1/solr/search?q={quote_plus(q)}&location={quote_plus(country)}&page={page}"
                    status, data = http_json(url, headers={"x-origin": b["origin"]})
                    if status != 200 or not data:
                        break
                    rows = data.get("jobs") or []
                    for r in rows:
                        gid = r.get("guid") or ""
                        if not gid or gid in seen:
                            continue
                        seen.add(gid)
                        loc = r.get("location_exact") or ""
                        c = guess_country(loc) or guess_country(r.get("country_exact") or "")
                        if c and c not in countries(profile):
                            continue
                        city = re.sub(r"[^a-z0-9]+", "-", (r.get("city_exact") or "").lower()).strip("-") or "job"
                        iso3 = (loc.split(",")[-1].strip().lower() or "x")
                        slug = re.sub(r"[^a-z0-9]+", "-", (r.get("title_exact") or "").lower()).strip("-")
                        out.append(job(r.get("title_exact"), b["company"], loc, f"https://{b['origin']}/{city}-{iso3}/{slug}/{gid}/job/",
                                       r.get("description") or "", "Career site", c, (r.get("date_new") or "")[:10]))
                    if not (data.get("pagination") or {}).get("has_more_pages"):
                        break
                    time.sleep(0.5)
        log(f".jobs {b['company']}: {len(seen)} listings")
    return out


# ---------------------------------------------------------------- Rigzone (oil & gas job board, free)
RIGZONE_COUNTRIES = {"Saudi Arabia": "Saudi Arabia", "United Arab Emirates": "United Arab Emirates", "Qatar": "Qatar",
                     "Kuwait": "Kuwait", "Oman": "Oman", "Bahrain": "Bahrain", "Canada": "Canada", "Australia": "Australia"}


def fetch_rigzone(cfg: dict, profile: dict) -> list[dict]:
    """Rigzone search (the same JSON call its page makes). ~20 rows per page, IndustryExp = years asked for."""
    if not cfg.get("enabled"):
        return []
    out, seen = [], set()
    pages = int(cfg.get("pages", 2))
    for country in countries(profile):
        rc = RIGZONE_COUNTRIES.get(country)
        if not rc:
            continue
        for q in cfg.get("queries") or ["civil engineer", "site engineer", "graduate engineer", "field engineer", "HSE officer"]:
            got = 0
            for page in range(1, pages + 1):
                form = (f"sk={quote_plus(q)}&sl={quote_plus(rc)}&country={quote_plus(rc)}&page={page}&radius=155&unit=MI"
                        "&region=0&category=0&emptype=0&experience=0&skill=0&ll=&state=&IsAjaxCallJobSearch=true")
                status, body = http("https://www.rigzone.com/oil/jobs/searchAjax", data=form.encode(),
                                    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
                if status != 200 or not body:
                    log(f"Rigzone: HTTP {status} on '{q}' {country}")
                    break
                try:
                    data = json.loads(body)
                except ValueError:
                    break
                rows = data.get("SolrJobs") or []
                total = int(data.get("NumFound") or 0)
                for r in rows:
                    pid = r.get("PostingID")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    loc = clean(r.get("JobLocation", "")).replace(" ", " ")
                    exp = r.get("IndustryExp") or 0
                    desc = clean(r.get("JobDescription", ""))
                    if exp:
                        desc += f" Experience asked: {exp}+ years."
                    posted = ""
                    try:
                        from datetime import datetime
                        posted = datetime.strptime(r.get("PostedDate", ""), "%B %d, %Y").date().isoformat()
                    except Exception:
                        pass
                    out.append(job(r.get("JobTitle"), r.get("CompanyName"), loc, "https://www.rigzone.com" + r.get("PostingUrl", ""),
                                   desc, "Rigzone", guess_country(loc) or country, posted))
                got += len(rows)
                if not rows or got >= total:
                    break
                time.sleep(0.6)
    log(f"Rigzone: {len(out)} rows")
    return out


def enrich_rigzone(j: dict) -> None:
    status, body = http(j["url"])
    if status == 200:
        m = re.search(r'<div[^>]+class="[^"]*job-description[^"]*"[^>]*>([\s\S]*?)</div>\s*</div>', body) or \
            re.search(r'<section[^>]+id="job-description"[^>]*>([\s\S]*?)</section>', body) or \
            re.search(r'<div class="description">([\s\S]*?)</div>\s*</article>', body)
        if m:
            j["description"] = clean(m.group(1))[:6000]


# ---------------------------------------------------------------- Newspaper job ads (WordPress sites that republish Gulf newspaper vacancies)
def fetch_wordpress(cfg: dict, profile: dict) -> list[dict]:
    """Sites built on WordPress expose /wp-json/wp/v2/posts?search=... - used for Gulf Job Paper, which republishes the
    newspaper recruitment pages (Assignments Abroad Times, Gulf Job Paper) every day."""
    out = []
    days = int(cfg.get("days", 14))
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    for site in cfg.get("sites", []):
        n = 0
        for q in cfg.get("queries") or ["civil engineer", "site engineer", "graduate engineer", "oil gas civil", "HSE officer", "storekeeper", "quantity surveyor"]:
            url = (f"{site['base'].rstrip('/')}/wp-json/wp/v2/posts?search={quote_plus(q)}&per_page={int(cfg.get('per_query', 30))}"
                   "&orderby=date&_fields=id,date,link,title,excerpt,content")
            status, data = http_json(url)
            if status != 200 or not isinstance(data, list):
                log(f"{site['name']}: HTTP {status}")
                break
            for p in data:
                d = (p.get("date") or "")[:10]
                if d < cutoff:
                    continue
                title = clean((p.get("title") or {}).get("rendered", ""))
                text = clean((p.get("content") or {}).get("rendered", "")) or clean((p.get("excerpt") or {}).get("rendered", ""))
                # the posting often names several countries; guess from the title first, then the text
                country = guess_country(title) or guess_country(text[:400])
                if country and country not in countries(profile):
                    continue
                company = ""
                m = re.search(r"\b(?:company|client|employer)\s*(?:name)?\s*[:\-]\s*([A-Z][\w&.,' ]{3,60}?)(?=\s{2}|[.,;\n]|\s+(?:location|job|position|salary|email)\b)", text)
                if m and not re.search(r"\b(aed|sar|qar|kwd|omr|salary|free|food|accommodation|interview|diploma|urgent|walk)\b", m.group(1), re.I):
                    company = m.group(1).strip(" .,")
                out.append(job(title, company or "Unknown", country or "Not specified", p.get("link", ""), text[:4000], site["name"], country, d))
                n += 1
            time.sleep(0.4)
        log(f"{site['name']} (newspaper ads): {n} posts")
    return out

# ---------------------------------------------------------------- Oracle Recruiting Cloud (many large Gulf employers)
ORC_QUERIES = ["civil", "site engineer", "QA/QC", "planning", "quantity surveyor", "HSE", "storekeeper", "graduate"]


def fetch_oracle(cfg: dict, profile: dict) -> list[dict]:
    """Public JSON behind Oracle's candidate-experience career sites: <base>/hcmUI/CandidateExperience/en/sites/<site>."""
    out = []
    for b in cfg.get("boards", []):
        base, site = b["base"].rstrip("/"), b.get("site", "CX_1")
        seen = set()
        for q in b.get("queries") or ORC_QUERIES:
            for page in range(int(cfg.get("pages", 2))):
                finder = (f'findReqs;siteNumber={site},facetsList=LOCATIONS,limit=25,offset={page * 25},'
                          f'keyword="{q}",sortBy=POSTING_DATES_DESC')
                url = (f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
                       f"&expand=requisitionList.secondaryLocations&finder={quote(finder, safe='=;,')}")
                status, data = http_json(url)
                if status != 200 or not data or not data.get("items"):
                    if status not in (200, 0):
                        log(f"Oracle {b['company']}: HTTP {status}")
                    break
                item = data["items"][0]
                reqs = item.get("requisitionList") or []
                for r in reqs:
                    rid = str(r.get("Id") or "")
                    if not rid or rid in seen:
                        continue
                    seen.add(rid)
                    loc = r.get("PrimaryLocation") or ""
                    country = guess_country(f"{loc}, {r.get('PrimaryLocationCountry') or ''}")
                    if country and country not in countries(profile):
                        continue
                    out.append(job(r.get("Title"), b["company"], loc, f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                                   r.get("ShortDescriptionStr") or "", "Career site", country, (r.get("PostedDate") or "")[:10]))
                if len(reqs) < 25 or (page + 1) * 25 >= int(item.get("TotalJobsCount") or 0):
                    break
                time.sleep(0.6)
        log(f"Oracle {b['company']}: {len(seen)} listings")
    return out


def enrich_oracle(j: dict) -> None:
    m = re.match(r"(https://[^/]+)/hcmUI/CandidateExperience/en/sites/([^/]+)/job/(\d+)", j["url"])
    if not m:
        return
    base, site, rid = m.groups()
    finder = quote(f'ById;Id="{rid}",siteNumber={site}', safe="=;,")
    status, data = http_json(f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder={finder}")
    if status == 200 and data and data.get("items"):
        it = data["items"][0]
        j["description"] = clean(" ".join(str(it.get(k) or "") for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))[:6000]


ENRICHERS = {"Workday": enrich_workday, "SuccessFactors": enrich_successfactors, "Workable": enrich_workable,
             "LinkedIn": enrich_linkedin, **{n: sitebrowse.enrich for n in sitebrowse.DETAIL_SOURCES}, "Rigzone": enrich_rigzone, "Career site": lambda j: enrich_career(j)}


def enrich_smartrecruiters(j: dict) -> None:
    m = re.match(r"https://jobs\.smartrecruiters\.com/([^/]+)/(\d+)", j["url"])
    if not m:
        return
    status, data = http_json(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}")
    secs = ((data or {}).get("jobAd") or {}).get("sections") or {}
    text = " ".join(clean((secs.get(k) or {}).get("text") or "") for k in ("jobDescription", "qualifications", "additionalInformation"))
    if status == 200 and text.strip():
        j["description"] = text[:6000]


def enrich_phenom(j: dict) -> None:
    """Phenom job pages carry the ad as a JSON string inside the page's data block."""
    status, body = http(j["url"])
    m = re.search(r'"description":("(?:[^"\\]|\\.){200,}?")', body or "") if status == 200 else None
    if m:
        try:
            raw = json.loads(m.group(1))
        except ValueError:
            return
        j["description"] = clean(html.unescape(raw))[:6000]


def enrich_eightfold(j: dict) -> None:
    m = re.match(r"https://((?:jobs|careers)\.([^/]+))/careers/job/(\d+)", j["url"])
    if not m:
        return
    host, domain, pid = m.groups()
    status, data = http_json(f"https://{host}/api/apply/v2/jobs/{pid}?domain={quote(domain)}")
    d = (data or {}).get("data", data) or {}
    if status == 200 and d.get("job_description"):
        j["description"] = clean(d["job_description"])[:6000]


def enrich_career(j: dict) -> None:
    """Employer career sites: pick the reader from the link."""
    u = j["url"]
    if "jobs.smartrecruiters.com/" in u:
        enrich_smartrecruiters(j)
    elif re.search(r"/[a-z]{2}/[a-z]{2}/job/\d", u):
        enrich_phenom(j)
    elif "/careers/job/" in u:
        enrich_eightfold(j)
    elif re.search(r"https://[^/]+\.jobs/", u):
        return                                         # .jobs listings already carry the full ad
    elif ".myworkdayjobs.com/" in u:
        enrich_workday(j)
    elif "/hcmUI/CandidateExperience/" in u:
        enrich_oracle(j)
    elif "apply.workable.com/" in u:
        enrich_workable(j)
    else:
        enrich_successfactors(j)


def enrich(j: dict) -> None:
    """Fetch the full posting text for sources that only gave a title in the listing."""
    fn = ENRICHERS.get(j["source"])
    if fn and (len(j.get("description", "")) < 300 or j.get("_snippet")):
        try:
            fn(j)
        except Exception as e:  # never let one posting kill the run
            log(f"  enrich failed {j['url'][:60]}: {e}")


def fetch_all(sources: dict, profile: dict) -> list[dict]:
    """Each source is a different website, so they run side by side; inside a source requests stay one at a time
    with polite pauses."""
    from concurrent.futures import ThreadPoolExecutor
    fetchers = [
        ("linkedin_guest", fetch_linkedin), ("browser_sites", sitebrowse.fetch), ("seek", fetch_seek), ("workday", fetch_workday), ("rigzone", fetch_rigzone),
        ("newspapers", fetch_wordpress), ("successfactors", fetch_successfactors), ("workable", fetch_workable),
        ("jooble", fetch_jooble), ("gulftalent", fetch_gulftalent), ("google_alerts_rss", fetch_rss),
        ("greenhouse", fetch_greenhouse), ("lever", fetch_lever), ("smartrecruiters", fetch_smartrecruiters),
        ("oracle", fetch_oracle), ("phenom", fetch_phenom), ("eightfold", fetch_eightfold), ("jobsyn", fetch_jobsyn),
    ]

    def run(item):
        key, fn = item
        t0 = time.time()
        try:
            got = [j for j in fn(sources.get(key) or {}, profile) if j.get("url") and j.get("title")]
        except Exception as e:
            log(f"{key}: failed ({type(e).__name__}: {e})")
            got = []
        if time.time() - t0 > 5:
            log(f"  ({key}: {len(got)} jobs in {int(time.time() - t0)}s)")
        return got
    jobs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for got in ex.map(run, fetchers):
            jobs.extend(got)
    return jobs


def enrich_many(jobs: list[dict], budget: int) -> int:
    """Fetch full ads for up to `budget` jobs (best first). One worker per website, so each site still sees one
    request at a time. Returns how many were fetched."""
    from concurrent.futures import ThreadPoolExecutor
    picked = [j for j in jobs if j.get("source") in ENRICHERS and (len(j.get("description", "")) < 300 or j.get("_snippet"))][:budget]
    from urllib.parse import urlparse
    by_site: dict[str, list] = {}
    for j in picked:                                   # group by website host (career sites are many different hosts)
        by_site.setdefault(urlparse(j["url"]).netloc.lower(), []).append(j)

    def work(lst):
        n = 0
        for j in lst:
            before = len(j.get("description", ""))
            enrich(j)
            n += len(j.get("description", "")) != before
        return n
    with ThreadPoolExecutor(max_workers=max(1, min(10, len(by_site)))) as ex:
        return sum(ex.map(work, by_site.values()))
