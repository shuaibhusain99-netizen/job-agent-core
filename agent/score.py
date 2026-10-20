"""Score a job against profile.json. Transparent rules, 0-100, with the reasons recorded."""
from __future__ import annotations
import re
from datetime import date
from functools import lru_cache
from .util import countries, country_weight, years_experience, VAGUE

NUM = r"(\d{1,2})(?:\.\d+)?"                  # '3.5 years' counts as 3
YRS = r"(?:years?|yrs?)"
YEARS_RANGE = re.compile(rf"\b{NUM}\s*(?:-|–|—|to|and)\s*{NUM}\s*\+?\s*{YRS}", re.I)          # 5-7 years, between 2 and 4 years
YEARS_MIN = re.compile(rf"\b(?:minimum|min\.?|at least|more than|over)\s+(?:of\s+)?{NUM}\s*\+?\s*{YRS}", re.I)
YEARS_PLUS = re.compile(rf"\b{NUM}\s*\+\s*{YRS}", re.I)
YEARS_ANY = re.compile(rf"\b{NUM}\s*{YRS}", re.I)
# 'Years of experience: 7', 'Experience (years): 3 - 5', 'Years of relevant experience required - 4'
YEARS_AFTER = re.compile(rf"\b{YRS}\b[^.:;\n]{{0,40}}?\bexperience\b[^.:;\n]{{0,25}}?[:=\-–]\s*{NUM}(?:\s*(?:-|–|to)\s*{NUM})?(?!\s*%)|"
                         rf"\bexperience\s*\({YRS}\)\s*[:=\-–]?\s*{NUM}(?:\s*(?:-|–|to)\s*{NUM})?", re.I)
EXP_WORD = re.compile(r"\bexperience|\bexp\b|\bbackground\b|\bpost[- ]?qualification|\bpost[- ]?graduat", re.I)
# sentences about the employer, not the candidate: "a contractor with over 25 years of experience in the Kingdom"
COMPANY_TALK = re.compile(r"\b(we|our|us|company|firm|group|organi[sz]ation|established|founded|history|heritage|track record|"
                          r"legacy|in business|serving|has (over|more than)|have (over|more than)|since \d{4}|brings|boasts|"
                          r"operating|in operation|has been|have been|leading|leader|pioneer)\b", re.I)
# ... unless the same sentence clearly asks something of the candidate: "we need a site engineer with 3-5 years"
REQ_TALK = re.compile(r"\b(need|needs|require[sd]?|must|should|candidates?|applicants?|looking for|seeking|ideal|you (have|bring|possess|hold)|"
                      r"possess|hold|holding)\b", re.I)


WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
           "twelve": 12, "fifteen": 15}


def _prep_years(text: str) -> str:
    """'three (3) years' / 'five years' -> numbers; 'Experience\n2 to 5 Years' (label above the value) -> one line."""
    t = re.sub(r"\*\*|__|\*", " ", text)
    t = re.sub(r"\b(\w+)\s*\(\s*(\d{1,2})\s*\+?\s*\)\s*(?=years?|yrs?)", r"\2 ", t, flags=re.I)
    t = re.sub(r"\b(" + "|".join(WORDNUM) + r")\b(?=\s*(?:\+\s*)?(?:years?|yrs?)\b)", lambda m: str(WORDNUM[m.group(1).lower()]), t, flags=re.I)
    return re.sub(r"(experience)\s*:?\s*\n\s*(?=\d{1,2}\s*(?:\+|-|–|to|years?\b|yrs?\b))", r"\1: ", t, flags=re.I)


def required_years(text: str, company: str = "") -> tuple[int | None, int | None]:
    """(min, max) years of experience the posting asks for. Only sentences about the candidate's experience count:
    'a company with 25 years of history', 'a 2-year contract' and 'we have 30 years of experience' are ignored -
    judged on the words just before the number, so a long paragraph that mentions 'we' elsewhere still counts."""
    co = re.sub(r"\s+(contracting|company|co|llc|ltd|group|corporation|inc)\b.*$", "", (company or "").lower()).strip()
    co = co if len(co) >= 3 and co not in ("unknown",) else ""
    text = _prep_years(text)
    # sentence ends: . ; newline, bullets, and a spaced dash/plus that is NOT between two numbers ('5 - 7 years' stays whole)
    for sent in re.split(r"\.(?!\d)|[;\n•●▪]|(?<!\d)\s[-+]\s(?!\d)", text):
        if not EXP_WORD.search(sent):
            continue
        for rx in (YEARS_RANGE, YEARS_MIN, YEARS_PLUS, YEARS_AFTER, YEARS_ANY):
            for m in rx.finditer(sent):
                nums = [g for g in m.groups() if g is not None]
                if not nums:
                    continue
                before = sent[max(0, m.start() - 60):m.start()]
                if int(nums[0]) > 15 or (COMPANY_TALK.search(before) and not REQ_TALK.search(before)) or (co and co in before.lower()):
                    continue
                lo = int(nums[0])
                hi = int(nums[1]) if len(nums) > 1 and int(nums[1]) >= lo else None
                return lo, hi
    return None, None


# ------------------------------------------------------------------ pay
CURRENCY = r"(?:SAR|SR|AED|QAR|QR|KWD|KD|OMR|RO|BHD|BD|CAD|AUD|USD|US\$|C\$|A\$|\$|£|€|riyals?|dirhams?|dinars?|dollars?)"
NUMBER = r"\d{1,3}(?:\.\d)?\s*[kK]\b|\d{1,3}(?:[,.]\d{3})+(?:\.\d+)?|\d{3,6}(?:\.\d+)?"
SALARY_RE = re.compile(
    rf"({CURRENCY})\s*({NUMBER})(?:\s*(?:-|–|to)\s*{CURRENCY}?\s*({NUMBER}))?"
    rf"|({NUMBER})(?:\s*(?:-|–|to)\s*({NUMBER}))?\s*({CURRENCY})", re.I)
SALARY_CTX = re.compile(r"salary|per month|monthly|per annum|per year|p\.a\.|package|compensation|\bpay\b|remuneration|"
                        r"/month|/year|/hr|per hour|a month|a year|\+ ?super|ctc", re.I)
YEARLIKE = re.compile(r"^(19|20)\d\d$")


def find_salary(text: str) -> str:
    """The first money amount next to a salary-like word, e.g. 'SAR 8,000 - 10,000 per month' or '$160k - $180k'."""
    for m in SALARY_RE.finditer(text):
        nums = [g for g in (m.group(2), m.group(3), m.group(4), m.group(5)) if g]
        if not nums or all(YEARLIKE.match(n.strip()) for n in nums):
            continue
        window = text[max(0, m.start() - 80): m.end() + 80]
        if SALARY_CTX.search(window):
            return re.sub(r"\s+", " ", m.group(0)).strip()[:40]
    return ""


PAY_FLOOR = {"AED": 6000, "SAR": 6000, "SR": 6000, "QAR": 6000, "QR": 6000, "KWD": 500, "KD": 500, "OMR": 600, "RO": 600}


def _pay_below_floor(sal: str) -> bool:
    """A monthly Gulf salary clearly below an engineer's (yearly amounts are left alone)."""
    m = re.search(r"\b(AED|SAR|SR|QAR|QR|KWD|KD|OMR|RO)\b", sal or "", re.I)
    if not m:
        return False
    nums = [float(x.replace(",", "")) * (1000 if k else 1) for x, k in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?", sal)]
    return bool(nums) and max(nums) < PAY_FLOOR[m.group(1).upper()]


# ------------------------------------------------------------------ application e-mail
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
EMAIL_SKIP = re.compile(r"noreply|no-reply|donotreply|do-not-reply|unsubscribe|privacy|abuse@|postmaster@|webmaster@|@linkedin|@indeed|"
                        r"@glassdoor|@rigzone|@gulfjobpaper|@seek\.|@jooble|example\.(com|org)|@sentry|@google|@wixpress|@sentry\.io|"
                        r"@domain\.|@email\.com$|@yourcompany", re.I)
IMAGE_TLD = re.compile(r"\.(png|jpe?g|gif|svg|webp|bmp|ico|css|js)$", re.I)


def _deobfuscate(text: str) -> str:
    t = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.I)
    return re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", t, flags=re.I)


def find_apply_email(text: str) -> str:
    """Up to three recruitment e-mail addresses from the posting (comma-separated)."""
    found = []
    for m in EMAIL_RE.finditer(_deobfuscate(text)):
        e = m.group(0).strip(".").lower()
        if EMAIL_SKIP.search(e) or IMAGE_TLD.search(e) or re.search(r"@\d+x\.", e) or e in found:
            continue
        found.append(e)
    return ",".join(found[:3])


# ------------------------------------------------------------------ sector, title rules
def find_sector(j: dict, profile: dict) -> str:
    text = (j["title"] + " " + j["company"] + " " + j.get("description", "")).lower()
    for key, sec in (profile.get("sectors") or {}).items():
        if key.startswith("_"):
            continue
        if any(re.search(p, text, re.I) for p in sec.get("patterns", [])):
            return key
    return ""


def find_level(j: dict, profile: dict) -> str:
    """starter / support / match / senior - from the title first, then the experience the ad asks for."""
    lv = profile.get("levels") or {}
    t = j["title"].lower()
    m = re.match(r"seniority level:\s*([a-z -]+)\.", j.get("description", "").lower())     # LinkedIn's own label
    if m and m.group(1).strip() in ("director", "executive"):
        return "senior"
    if m and m.group(1).strip() in ("entry level", "internship") and not (lv.get("senior") and re.search(lv["senior"], t, re.I)):
        return "starter"
    for name in ("senior", "starter", "support"):
        if lv.get(name) and re.search(lv[name], t, re.I):
            return name
    lo, _ = required_years((j["title"] + " " + j.get("description", "")).lower())
    if lo is not None:
        yrs = years_experience(profile)
        if lo <= 2 and yrs > 2:
            return "starter"
        if lo >= yrs + 3:
            return "senior"
    if lv.get("starter") and re.search(lv["starter"], j.get("description", "")[:600], re.I):
        return "starter"
    return "match"


def _word(term: str) -> str:
    """A whole word or phrase, plural allowed: 'site engineer' also matches 'Site Engineers'."""
    return r"(?<![a-z])" + re.escape(term) + r"(?:s|es)?(?![a-z])"


@lru_cache(maxsize=20000)
def _rx(term: str):
    return re.compile(_word(term))


def norm_title(title: str) -> str:
    """Spelling variants of the same title: 'QAQC', 'QA-QC', 'QA / QC' -> 'qa/qc'."""
    t = title.lower()
    t = re.sub(r"\bq\.?\s*a\.?\s*[/&-]?\s*q\.?\s*c\.?(?![a-z])", "qa/qc", t)
    return re.sub(r"\s+", " ", t)


CIVILISH = re.compile(r"\b(civil|water|wastewater|drainage|hydraulic|roads?|highways?|structural|infrastructure|utilit(y|ies)|"
                      r"pavements?|geotech\w*|sewer\w*|storm ?water|pipelines?|bridges?|earthworks?|municipal)\b")


def _all_targets(profile: dict) -> list[str]:
    out = list(profile["target_titles"])
    for k, c in profile.get("categories", {}).items():
        if not k.startswith("_"):
            out += c.get("target_titles", [])
    return out


def _penalties(title: str, profile: dict, matched: list[str]) -> list[tuple[str, int]]:
    """Title penalties that apply: whole words, not part of the matched target title, not exempted for this title."""
    exempt = profile.get("title_penalty_exempt", {})
    out = []
    for term, pen in profile["title_penalties"].items():
        if term.startswith("_") or not isinstance(pen, (int, float)) or any(term in t for t in matched):
            continue
        if _rx(term).search(title) and not (term in exempt and re.search(exempt[term], title, re.I)):
            out.append((term, int(pen)))
    return out


def title_only_reject(title: str, profile: dict) -> str | None:
    """Reason a title alone rules a job out (nationals-only, another discipline), or None."""
    t = norm_title(title)
    for pat in profile.get("title_reject_patterns", []):
        if re.search(pat, t, re.I):
            return f"title rejects: /{pat}/"
    matched = [x for x in _all_targets(profile) if _rx(x).search(t)]
    heavy = [(term, p) for term, p in _penalties(t, profile, matched) if p <= -50]
    return f"title:{heavy[0][0]}({heavy[0][1]})" if heavy else None


NEGATION = re.compile(r"\b(no|not|unable|cannot|can't|won't|will not|don't|do not|does not|doesn't|isn't|aren't|without|ineligible|"
                      r"not eligible|neither|nor|excluding)\b", re.I)


def _affirmed(text: str, m) -> bool:
    """A sponsorship phrase that isn't negated by the words around it ('we are unable to offer visa sponsorship')."""
    return not (NEGATION.search(text[max(0, m.start() - 40):m.start()]) or NEGATION.search(text[m.end():m.end() + 25]))


def _days_old(posted: str) -> int | None:
    try:
        return (date.today() - date.fromisoformat(posted[:10])).days
    except Exception:
        return None


# ------------------------------------------------------------------ scoring
def score(j: dict, profile: dict) -> tuple[int, str, list[str]]:
    """Score against every category, keep the best. Sets j["category"], j["sector"], j["salary"], j["apply_email"].
    Returns (score, verdict, reasons). verdict: 'apply' | 'maybe' | 'reject'."""
    j["sector"] = find_sector(j, profile)
    best = None
    cats = {"civil": {"target_titles": profile["target_titles"], "keyword_points": profile["keyword_points"]}}
    for key, c in profile.get("categories", {}).items():
        if not key.startswith("_"):
            cats[key] = c
    for key, c in cats.items():
        if c.get("requires_sector") and j["sector"] != c["requires_sector"]:
            continue                               # e.g. the Oil & Gas list only takes jobs that really are oil & gas
        s, v, r = _score_one(j, profile, key, c)
        rank = (s, "title not in category" not in r)   # on a tie (e.g. all 0), keep the category whose title matched:
        if best is None or rank > best[4]:             # its reason ("asks 8+ yrs") explains the result, "not in category" doesn't
            best = (s, v, r, key, rank)
    j["category"] = best[3]
    j["level"] = find_level(j, profile)
    verdict = best[1]
    reasons = list(best[2])
    text = (j["title"] + " " + j.get("description", "")).lower()
    j.pop("_cap", None)                            # the best verdict allowed whatever the AI reviewer later says
    if j["level"] == "senior":
        lo, _ = required_years(text, j.get("company", ""))
        if lo is None or lo > years_experience(profile) + 1:
            j["_cap"] = "maybe"
            if verdict == "apply":
                verdict = "maybe"                  # senior / lead titles usually want far more years: worth a look, not a send
                reasons.insert(1, "senior title - check the years asked")     # early, so a shortened note still says it
    wr = (profile.get("work_rights") or {}).get(j.get("country") or "", {})
    if wr.get("visa_needed"):
        no = wr.get("no_sponsor") and re.search(wr["no_sponsor"], text, re.I)
        yes = any(_affirmed(text, m) for sig in wr.get("sponsor_signals", []) for m in re.finditer(sig, text, re.I))
        if no or not yes:
            j["_cap"] = "maybe"
            if verdict == "apply":
                verdict = "maybe"                  # you need a sponsored work visa there: To apply only when they can sponsor
                reasons.insert(1, "needs a work visa - the ad doesn't say they sponsor")
        elif verdict == "apply":
            reasons.insert(1, "may sponsor a visa")
    if not j.get("country") and verdict == "apply":
        verdict = "maybe"                          # where the job is isn't known: worth a look, not a send
        reasons.insert(1, "country unknown - check where the job is")
    if not j.get("country"):
        j["_cap"] = "maybe"
    j["salary"] = find_salary(j["title"] + " " + j.get("description", ""))
    low = _pay_below_floor(j["salary"])
    if low and verdict == "apply":
        verdict = "maybe"                          # a monthly pay far below an engineer's
        reasons.insert(1, f"pay looks low ({j['salary']})")
        j["_cap"] = "maybe"
    j["apply_email"] = find_apply_email(j.get("description", ""))
    tags = (["sector:" + j["sector"]] if j["sector"] else []) + (["pay: " + j["salary"]] if j["salary"] else []) \
        + (["apply by e-mail"] if j["apply_email"] else [])
    at = next((i for i, r in enumerate(reasons) if r.startswith("keywords:")), len(reasons))
    reasons[at:at] = tags                          # short facts before the long keyword list, so a shortened note keeps them
    return best[0], verdict, reasons


def _score_one(j: dict, profile: dict, cat: str, c: dict) -> tuple[int, str, list[str]]:
    title = norm_title(j["title"])
    desc = j.get("description", "")
    text = (j["title"] + " " + desc).lower()
    reasons: list[str] = [f"category:{cat}"]
    pts = 0

    # 0. Titles that exclude you outright (nationals-only roles etc.)
    for pat in profile.get("title_reject_patterns", []):
        if re.search(pat, title, re.I):
            return 0, "reject", reasons + [f"title rejects: /{pat}/"]

    # 1. Title match (the strongest signal)
    matched = [t for t in c["target_titles"] if _rx(t).search(title)]
    civilish = False
    if matched:
        pts += 40
        reasons.append("target title")
    elif cat == "civil" and "engineer" in title and CIVILISH.search(title):
        pts += 30                                  # 'Drainage Engineer', 'Civil Design Engineer', 'Water & Wastewater Engineer'
        civilish = True
        reasons.append("civil-type engineer title")
    elif cat == "civil" and "engineer" in title:
        pts += 15
        reasons.append("engineer title")
    else:
        return 0, "reject", reasons + ["title not in category"]

    # 2. Title penalties (seniority, discipline)
    for term, pen in _penalties(title, profile, matched):
        pts += pen
        reasons.append(f"title:{term}({pen})")

    # 3. Hard rejects anywhere in the text (closed ads, PhD required, nationals only ...). A newspaper page lists several
    #    positions: a discipline rule for one of them (e.g. 'degree in electrical engineering') must not reject the page.
    multi = j.get("source") in profile.get("multi_position_sources", [])
    for pat in profile["hard_reject_patterns"]:
        if multi and "electrical|mechanical" in pat:
            continue
        if re.search(pat, text, re.I):
            return 0, "reject", reasons + [f"hard reject: /{pat}/"]
    age = _days_old(j.get("posted", ""))
    max_age = int(profile["search"].get("max_posting_age_days", 45))
    if age is not None and age > max_age:
        return 0, "reject", reasons + [f"posted {age} days ago"]

    # 4. Work rights outside the GCC (Australia, Canada): citizens/PR-only and security-clearance roles are out
    wr = (profile.get("work_rights") or {}).get(j.get("country") or "", {})
    for pat in wr.get("reject", []):
        if re.search(pat, text, re.I):
            return 0, "reject", reasons + [f"work rights: /{pat}/"]
    for pat in wr.get("penalty", []):
        if re.search(pat, text, re.I):
            pts -= 20
            reasons.append("needs local work rights")
            break

    # 5. Experience fit (only what is asked of the candidate - not the company's own history)
    yrs = years_experience(profile)
    lo, hi = required_years(text, j.get("company", ""))
    if lo is not None and lo >= max(7, yrs + 3):
        return 0, "reject", reasons + [f"asks {lo}+ years"]
    if lo is not None:
        if lo <= yrs and (hi is None or hi >= yrs):
            pts += 15
            reasons.append(f"experience fits ({lo}-{hi or '+'} yrs)")
        elif lo <= yrs:
            pts += 12          # junior / fresher range (e.g. 0-2 yrs): over-qualified is still a real option
            reasons.append(f"junior range ({lo}-{hi} yrs) - fits")
        elif lo <= yrs + 1:
            pts += 3
            reasons.append(f"experience close ({lo}+ yrs)")
        else:
            pts -= 30
            reasons.append(f"asks {lo}+ yrs")
    if re.search(r"\b(fresh(er|\s+graduate)s?|graduate (program|programme|trainee|engineer)|entry[- ]level|trainee|0\s*(-|to)\s*[12]\s*(years|yrs))\b", text):
        pts += 8
        reasons.append("starter role")

    # 6. Industry checks. Titles that exist in every industry (project engineer, stores, reporting ...) only count fully when
    #    the title, company or ad shows construction / infrastructure work. With only a listing snippet we can't tell yet:
    #    a small penalty, and the full ad (fetched for promising jobs) decides.
    ctx_text = text + " " + j.get("company", "").lower()
    short = len(desc) < 400 or bool(j.get("_snippet"))          # a listing teaser, however long, isn't the ad
    need = c.get("require_any")
    if need and not any(_rx(w).search(ctx_text) for w in need):
        pts -= 5 if short else 20
        reasons.append("industry unclear" if short else "not construction/utility/industrial")
    gen = profile.get("generic_titles") or {}
    if gen.get("context") and matched and all(t in gen.get("titles", []) for t in matched) \
            and not re.search(r"\bsite\b", title) and not re.search(gen["context"], ctx_text, re.I):
        pts -= 10 if short else 25
        reasons.append("industry unclear" if short else "not clearly civil / construction work")

    # 7. Keyword points (capped so a long description can't dominate)
    kw = 0
    hits = []
    for term, p in c["keyword_points"].items():
        if _rx(term).search(text):
            kw += p
            hits.append(term)
    pts += min(kw, 40)
    if short and (matched or civilish) and j.get("source") not in profile["search"].get("no_listing_bonus_sources", []):
        pts += 15          # only the listing snippet was available: don't punish for keywords it couldn't contain
        reasons.append("listing only")
    if hits:
        reasons.append("keywords: " + ", ".join(hits[:8]))

    # 8. Country. A stated location that isn't a target country = reject; unknown = small penalty.
    w = country_weight(profile, j.get("country") or "")
    if w is not None and j["country"] not in countries(profile):
        return 0, "reject", reasons + [f"country switched off: {j['country']}"]
    if w is None:
        if j.get("country") == "Other":
            return 0, "reject", reasons + [f"location outside target countries: {j['location']}"]
        loc = j.get("location") or ""
        vague = not loc or loc == "Not specified" or VAGUE.search(loc) or j.get("target_area")
        if not vague and not any(_rx(cn.lower()).search(text) for cn in countries(profile)):
            return 0, "reject", reasons + [f"location outside target countries: {j['location']}"]
        pts -= 5 if j.get("target_area") else 10      # target_area: the site itself only listed jobs in your countries
        reasons.append("country unknown")
    else:
        pts = int(pts * (0.85 + 0.15 * w))

    # 9. "Based in X" condition - flag, don't reject
    if re.search(r"\b(must be|currently|only)\s+(based|located|residing)\s+in\b", text):
        pts -= 8
        reasons.append("wants local candidates")

    s = max(0, min(100, pts))
    sp = profile["search"]
    threshold = sp["min_score"] if cat == "civil" else (sp.get("min_score_oilgas", sp["min_score"]) if cat == "oil_gas" else sp.get("min_score_other", sp["min_score"] + 5))
    verdict = "apply" if s >= threshold else ("maybe" if s >= threshold - 15 else "reject")
    return s, verdict, reasons
