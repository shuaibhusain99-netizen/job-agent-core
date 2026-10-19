"""Job fetchers. Every fetcher returns a list of job dicts (see util.job) and never raises."""
from __future__ import annotations
import json, os, re, time
from urllib.parse import quote, quote_plus, urljoin
from xml.etree import ElementTree as ET

from .util import http, http_json, job, clean, log, guess_country, countries


# ---------------------------------------------------------------- Jooble (free API key)
def fetch_jooble(cfg: dict, profile: dict) -> list[dict]:
    key = os.getenv("JOOBLE_API_KEY", "").strip()
    if not cfg.get("enabled") or not key:
        log("Jooble: skipped (no JOOBLE_API_KEY secret)")
        return []
    out = []
    budget = cfg.get("max_requests_per_run", 25)
    used = 0
    for country in countries(profile):
        if cfg.get("countries") and country not in cfg["countries"]:
            continue
        for q in cfg.get("queries") or profile["search"]["queries"]:
            for page in range(1, cfg.get("pages_per_query", 1) + 1):
                if used >= budget:
                    break
                used += 1
                status, data = http_json(f"https://jooble.org/api/{key}",
                                         {"keywords": q, "location": country, "page": page})
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
    out, challenged = [], False
    for country in countries(profile):
        if challenged:
            break
        for q in profile["search"]["queries"][:4]:
            url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
                   f"keywords={quote_plus(q)}&location={quote_plus(country)}&f_TPR=r604800&start=0")
            status, body = http(url)
            if status != 200 or "base-card" not in body:
                challenged = True
                break
            for card in re.findall(r"<li>[\s\S]*?</li>", body):
                t = re.search(r'base-search-card__title">\s*([^<]+)', card)
                c = re.search(r'class="hidden-nested-link"[^>]*>\s*([^<]+)', card)
                l = re.search(r'job-search-card__location">\s*([^<]+)', card)
                u = re.search(r'href="(https://[^"?]+)', card)
                d = re.search(r'datetime="([^"]+)"', card)
                if t and u:
                    out.append(job(t.group(1), c.group(1) if c else "", l.group(1) if l else country,
                                   u.group(1), "", "LinkedIn", country, d.group(1) if d else ""))
            time.sleep(1.5)
    log(f"LinkedIn: {'challenged - skipped' if challenged else f'{len(out)} rows'}")
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


# ---------------------------------------------------------------- Workday (employer ATS, reliable)
def fetch_workday(cfg: dict, profile: dict) -> list[dict]:
    out = []
    for b in cfg.get("boards", []):
        base = f"https://{b['tenant']}.{b['host']}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{b['tenant']}/{b['site']}/jobs"
        seen = set()
        for q in ["civil engineer", "site engineer", "project engineer", "QA/QC engineer", "infrastructure engineer"]:
            status, data = http_json(api, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q})
            if status != 200 or not data:
                if status:
                    log(f"Workday {b['company']}: HTTP {status}")
                break
            for p in data.get("jobPostings", []):
                path = p.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                loc = p.get("locationsText") or ""
                country = guess_country(loc)
                if country and country not in countries(profile):
                    continue
                url = f"{base}/{b['site']}{path}"
                out.append(job(p.get("title"), b["company"], loc, url, p.get("bulletFields", [""])[0] if p.get("bulletFields") else "",
                               "Workday", country, p.get("postedOn", "")))
            time.sleep(0.4)
        # Workday listings often omit the location; resolve it from the detail endpoint for
        # plausible titles so GCC postings aren't discarded as "country unknown".
        resolved = 0
        for j in out:
            if j["source"] != "Workday" or j["company"] != b["company"] or j["country"]:
                continue
            t = j["title"].lower()
            if any(w in t for w in profile["target_titles"]) and not any(w in t for w in ["senior", "principal", "lead", "manager", "director", "national"]):
                if resolved >= 40:
                    break
                enrich_workday(j)
                resolved += 1
                time.sleep(0.3)
        keep = [j for j in out if not (j["source"] == "Workday" and j["company"] == b["company"] and j["country"] and j["country"] not in countries(profile))]
        out[:] = keep
        log(f"Workday {b['company']}: {len(seen)} rows, {resolved} detail lookups")
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


# ---------------------------------------------------------------- SuccessFactors (employer ATS)
def fetch_successfactors(cfg: dict, profile: dict) -> list[dict]:
    out = []
    for b in cfg.get("boards", []):
        seen = set()
        for q in ["civil", "site engineer", "project engineer", "QA/QC", "infrastructure"]:
            url = f"{b['base']}/{b['slug']}/search/?q={quote_plus(q)}&locationsearch="
            status, body = http(url)
            if status != 200:
                log(f"SuccessFactors {b['company']}: HTTP {status}")
                break
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
        status, data = http_json(f"https://apply.workable.com/api/v3/accounts/{a['account']}/jobs",
                                 {"query": "engineer", "location": [], "department": [], "worktype": [], "remote": []})
        if status != 200 or not data:
            log(f"Workable {a['company']}: HTTP {status}")
            time.sleep(2)
            continue
        n = 0
        for r in data.get("results", []):
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


ENRICHERS = {"Workday": enrich_workday, "SuccessFactors": enrich_successfactors, "Workable": enrich_workable,
             "LinkedIn": enrich_linkedin}


def enrich(j: dict) -> None:
    """Fetch the full posting text for sources that only gave a title in the listing."""
    fn = ENRICHERS.get(j["source"])
    if fn and len(j.get("description", "")) < 300:
        try:
            fn(j)
        except Exception as e:  # never let one posting kill the run
            log(f"  enrich failed {j['url'][:60]}: {e}")


def fetch_all(sources: dict, profile: dict) -> list[dict]:
    fetchers = [
        ("jooble", fetch_jooble), ("google_alerts_rss", fetch_rss), ("gulftalent", fetch_gulftalent),
        ("linkedin_guest", fetch_linkedin), ("workday", fetch_workday), ("successfactors", fetch_successfactors),
        ("workable", fetch_workable), ("greenhouse", fetch_greenhouse), ("lever", fetch_lever),
        ("smartrecruiters", fetch_smartrecruiters),
    ]
    jobs = []
    for key, fn in fetchers:
        cfg = sources.get(key) or {}
        try:
            jobs.extend(fn(cfg, profile))
        except Exception as e:
            log(f"{key}: failed ({e})")
    return jobs
