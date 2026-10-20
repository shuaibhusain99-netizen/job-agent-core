"""Job fetchers. Every fetcher returns a list of job dicts (see util.job) and never raises."""
from __future__ import annotations
import json, os, re, time
from urllib.parse import quote, quote_plus, urljoin
from xml.etree import ElementTree as ET

from pathlib import Path
from .util import http, http_json, job, clean, log, guess_country, countries


# ---------------------------------------------------------------- Jooble (free API key)
JOOBLE_SITES = {"Saudi Arabia": "sa", "United Arab Emirates": "ae", "Qatar": "qa", "Kuwait": "kw", "Oman": "om",
                "Bahrain": "bh", "Canada": "ca", "Australia": "au"}


KEYS_PATH = Path(__file__).resolve().parents[1] / "keys.json"


def all_queries(profile: dict) -> list[str]:
    """civil + other + oil & gas search phrases, de-duplicated, order kept."""
    sp = profile["search"]
    out, seen = [], set()
    for q in sp["queries"] + sp.get("queries_other", []) + sp.get("queries_oilgas", []):
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
def fetch_linkedin(cfg: dict, profile: dict) -> list[dict]:
    if not cfg.get("enabled"):
        return []
    out, blocked_countries = [], 0
    pages = int(cfg.get("pages", 3))
    tpr = f"r{int(cfg.get('days', 7)) * 86400}"
    for country in countries(profile):
        country_hits, challenged = 0, 0
        civil_q = {x.lower() for x in profile["search"]["queries"]}
        for q in all_queries(profile):
            start = 0
            for page in range(pages if q.lower() in civil_q else 1):
                url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
                       f"keywords={quote_plus(q)}&location={quote_plus(country)}&f_TPR={tpr}&start={start}")
                status, body = http(url)
                if status == 200 and "base-card" not in body:
                    break                                   # no (more) results for this query
                if status != 200:
                    challenged += 1
                    time.sleep(3)
                    break
                cards = re.findall(r"<li>[\s\S]*?</li>", body)
                for card in cards:
                    t = re.search(r'base-search-card__title">\s*([^<]+)', card)
                    c = re.search(r'class="hidden-nested-link"[^>]*>\s*([^<]+)', card)
                    l = re.search(r'job-search-card__location">\s*([^<]+)', card)
                    u = re.search(r'href="(https://[^"?]+)', card)
                    d = re.search(r'datetime="([^"]+)"', card)
                    if t and u:
                        loc = l.group(1) if l else country
                        out.append(job(t.group(1), c.group(1) if c else "", loc,
                                       u.group(1), "", "LinkedIn", guess_country(clean(loc)) or country, d.group(1)[:10] if d else ""))
                        country_hits += 1
                time.sleep(1.2)
                if not cards:
                    break
                start += len(cards)
            if challenged >= 3:
                break
        log(f"LinkedIn {country}: {country_hits} rows" + (" (stopped: LinkedIn is limiting requests)" if challenged >= 3 else ""))
        blocked_countries = blocked_countries + 1 if challenged >= 3 else 0
        if blocked_countries >= 2:
            log("LinkedIn: limited in two countries in a row - trying again next run")
            break
    log(f"LinkedIn: {len(out)} rows total")
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
    time.sleep(1.2)


# ---------------------------------------------------------------- SEEK (Australia, public API)
def fetch_seek(cfg: dict, profile: dict) -> list[dict]:
    if not cfg.get("enabled") or "Australia" not in countries(profile):
        return []
    out, seen = [], set()
    for q in cfg.get("queries", ["civil engineer"]) + profile["search"].get("queries_other", []) + profile["search"].get("queries_oilgas", []):
        for page in range(1, int(cfg.get("pages", 2)) + 1):
            url = ("https://www.seek.com.au/api/jobsearch/v5/search?siteKey=AU-Main&sourcesystem=houston"
                   f"&keywords={quote_plus(q)}&page={page}&pageSize=50&locale=en-AU&sortmode=ListedDate")
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
                status, data = http_json(api, {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": q})
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
                                    "Workday", country, _wd_posted(p.get("postedOn", ""))))
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
                country = guess_country(loc)
                if country and country not in countries(profile):
                    continue
                out.append(job(title, b["company"], loc, urljoin(b["base"], href), "", "SuccessFactors", country))
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
            out.append(job(r.get("title"), a["company"], loc_txt, url, "", "Workable", country, (r.get("published") or "")[:10]))
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
            out.append(job(it.get("text"), b.get("company", b["site"]), cat.get("location", ""), it.get("hostedUrl"),
                           it.get("descriptionPlain", ""), "Lever"))
    return out


def fetch_smartrecruiters(cfg: dict, profile: dict) -> list[dict]:
    out = []
    for c in cfg.get("companies", []):
        status, data = http_json(f"https://api.smartrecruiters.com/v1/companies/{quote(c)}/postings?limit=100")
        if status != 200 or not data:
            continue
        for it in data.get("content", []):
            loc = it.get("location") or {}
            loc_txt = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
            out.append(job(it.get("name"), c, loc_txt, f"https://jobs.smartrecruiters.com/{c}/{it.get('id')}", "", "SmartRecruiters"))
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

ENRICHERS = {"Workday": enrich_workday, "SuccessFactors": enrich_successfactors, "Workable": enrich_workable,
             "LinkedIn": enrich_linkedin, "Rigzone": enrich_rigzone}


def enrich(j: dict) -> None:
    """Fetch the full posting text for sources that only gave a title in the listing."""
    fn = ENRICHERS.get(j["source"])
    if fn and len(j.get("description", "")) < 300:
        try:
            fn(j)
        except Exception as e:  # never let one posting kill the run
            log(f"  enrich failed {j['url'][:60]}: {e}")


def fetch_all(sources: dict, profile: dict) -> list[dict]:
    """Each source is a different website, so they run side by side; inside a source requests stay one at a time
    with polite pauses."""
    from concurrent.futures import ThreadPoolExecutor
    fetchers = [
        ("linkedin_guest", fetch_linkedin), ("seek", fetch_seek), ("workday", fetch_workday), ("rigzone", fetch_rigzone),
        ("newspapers", fetch_wordpress), ("successfactors", fetch_successfactors), ("workable", fetch_workable),
        ("jooble", fetch_jooble), ("gulftalent", fetch_gulftalent), ("google_alerts_rss", fetch_rss),
        ("greenhouse", fetch_greenhouse), ("lever", fetch_lever), ("smartrecruiters", fetch_smartrecruiters),
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
    with ThreadPoolExecutor(max_workers=6) as ex:
        for got in ex.map(run, fetchers):
            jobs.extend(got)
    return jobs


def enrich_many(jobs: list[dict], budget: int) -> int:
    """Fetch full ads for up to `budget` jobs (best first). One worker per website, so each site still sees one
    request at a time. Returns how many were fetched."""
    from concurrent.futures import ThreadPoolExecutor
    picked = [j for j in jobs if j.get("source") in ENRICHERS and len(j.get("description", "")) < 300][:budget]
    by_site: dict[str, list] = {}
    for j in picked:
        by_site.setdefault(j["source"], []).append(j)

    def work(lst):
        n = 0
        for j in lst:
            before = len(j.get("description", ""))
            enrich(j)
            n += len(j.get("description", "")) != before
        return n
    with ThreadPoolExecutor(max_workers=max(1, len(by_site))) as ex:
        return sum(ex.map(work, by_site.values()))
