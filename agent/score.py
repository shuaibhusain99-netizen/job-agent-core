"""Score a job against profile.json. Transparent rules, 0-100, with the reasons recorded."""
from __future__ import annotations
import re
from .util import countries, country_weight, years_experience

YEARS_RANGE = re.compile(r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I)
YEARS_MIN = re.compile(r"\b(?:minimum|min\.?|at least|more than|over)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I)
YEARS_PLUS = re.compile(r"\b(\d{1,2})\s*\+\s*(?:years|yrs)", re.I)
YEARS_ANY = re.compile(r"\b(\d{1,2})\s*(?:years|yrs)", re.I)


EXP_WORD = re.compile(r"\bexperience|\bexp\b|\bbackground\b|\bpost[- ]?qualification|\bpost[- ]?graduat", re.I)


def required_years(text: str) -> tuple[int | None, int | None]:
    """(min, max) years of experience the posting asks for. Only sentences that talk about experience count,
    so 'a company with 25 years of history' or 'a 2-year contract' are ignored."""
    for sent in re.split(r"[.;\n\u2022\u25cf\u25aa]|\s-\s", text):
        if not EXP_WORD.search(sent):
            continue
        m = YEARS_RANGE.search(sent)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = YEARS_MIN.search(sent) or YEARS_PLUS.search(sent) or YEARS_ANY.search(sent)
        if m:
            return int(m.group(1)), None
    return None, None


CURRENCY = r"(?:SAR|SR|AED|QAR|QR|KWD|KD|OMR|RO|BHD|BD|CAD|AUD|USD|US\$|C\$|A\$|\$|£|€|riyals?|dirhams?|dinars?|dollars?)"
NUMBER = r"\d{1,3}(?:[,.]\d{3})+|\d{4,6}|\d{1,3}\s*k"
SALARY_RE = re.compile(
    rf"({CURRENCY})\s*({NUMBER})(?:\s*(?:-|–|to)\s*({NUMBER}))?"
    rf"|({NUMBER})(?:\s*(?:-|–|to)\s*({NUMBER}))?\s*({CURRENCY})", re.I)
SALARY_CTX = re.compile(r"salary|per month|monthly|per annum|per year|p\.a\.|package|compensation|pay|remuneration|/month|/year|a month|a year", re.I)


def find_salary(text: str) -> str:
    """Return the first money amount that sits near a salary-like word, e.g. 'SAR 8,000 - 10,000 per month'."""
    for m in SALARY_RE.finditer(text):
        window = text[max(0, m.start() - 80): m.end() + 80]
        if SALARY_CTX.search(window):
            return re.sub(r"\s+", " ", m.group(0)).strip()[:40]
    return ""


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_SKIP = re.compile(r"noreply|no-reply|donotreply|unsubscribe|privacy|support@|info@linkedin|@linkedin|@indeed|@glassdoor|@rigzone|@gulfjobpaper|@seek|@jooble|example\.com|@sentry|@google", re.I)


def find_apply_email(text: str) -> str:
    """First e-mail address in the posting that looks like a recruitment inbox (comma-separated if several)."""
    found = []
    for m in EMAIL_RE.finditer(text):
        e = m.group(0).strip(".").lower()
        if EMAIL_SKIP.search(e) or e in found:
            continue
        found.append(e)
    return ",".join(found[:3])


def find_sector(j: dict, profile: dict) -> str:
    text = (j["title"] + " " + j["company"] + " " + j.get("description", "")).lower()
    for key, sec in (profile.get("sectors") or {}).items():
        if key.startswith("_"):
            continue
        if any(re.search(p, text, re.I) for p in sec.get("patterns", [])):
            return key
    return ""


def score(j: dict, profile: dict) -> tuple[int, str, list[str]]:
    """Score against every category, keep the best. Sets j["category"], j["sector"], j["salary"].
    Returns (score, verdict, reasons). verdict: 'apply' | 'maybe' | 'reject'."""
    best = None
    cats = {"civil": {"target_titles": profile["target_titles"], "keyword_points": profile["keyword_points"]}}
    for key, c in profile.get("categories", {}).items():
        if not key.startswith("_"):
            cats[key] = c
    for key, c in cats.items():
        s, v, r = _score_one(j, profile, key, c["target_titles"], c["keyword_points"])
        if best is None or s > best[0]:
            best = (s, v, r, key)
    j["category"] = best[3]
    j["sector"] = find_sector(j, profile)
    j["salary"] = find_salary(j["title"] + " " + j.get("description", ""))
    j["apply_email"] = find_apply_email(j.get("description", ""))
    if j["sector"]:
        best[2].append("sector:" + j["sector"])
    if j["salary"]:
        best[2].append("pay: " + j["salary"])
    if j["apply_email"]:
        best[2].append("apply by e-mail")
    return best[0], best[1], best[2]


def _score_one(j: dict, profile: dict, cat: str, target_titles: list, keyword_points: dict) -> tuple[int, str, list[str]]:
    title = j["title"].lower()
    text = (j["title"] + " " + j["description"]).lower()
    reasons: list[str] = [f"category:{cat}"]
    pts = 0

    # 0. Titles that exclude you outright (nationals-only roles etc.)
    for pat in profile.get("title_reject_patterns", []):
        if re.search(pat, title, re.I):
            return 0, "reject", reasons + [f"title rejects: /{pat}/"]

    # 1. Title match (the strongest signal)
    matched = [t for t in target_titles if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", title)]
    if matched:
        pts += 40
        reasons.append("target title")
    elif cat == "civil" and "engineer" in title:
        pts += 15
        reasons.append("engineer title")
    else:
        return 0, "reject", reasons + ["title not in category"]

    # 2. Title penalties (seniority, discipline, nationality restrictions)
    for term, pen in profile["title_penalties"].items():
        if term.startswith("_") or any(term in t for t in matched):
            continue          # e.g. 'surveyor' is not a penalty when the role IS quantity surveyor
        if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", title):
            pts += pen
            reasons.append(f"title:{term}({pen})")

    # 3. Hard rejects anywhere in the text
    for pat in profile["hard_reject_patterns"]:
        if re.search(pat, text, re.I):
            return 0, "reject", [f"hard reject: /{pat}/"]

    # 4. Experience fit
    yrs = years_experience(profile)
    lo, hi = required_years(text)
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

    # 5. Keyword points (capped so a long description can't dominate)
    kw = 0
    hits = []
    for term, p in keyword_points.items():
        if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text):
            kw += p
            hits.append(term)
    kw = min(kw, 40)
    pts += kw
    if len(j.get("description", "")) < 400 and "target title" in reasons:
        pts += 15          # listing text only (no full description available): don't punish for missing keywords
        reasons.append("listing only")
    if hits:
        reasons.append("keywords: " + ", ".join(hits[:8]))

    # 6. Country weight. A stated location that isn't a target country = reject; unknown = small penalty.
    w = country_weight(profile, j.get("country") or "")
    if w is not None and j["country"] not in countries(profile):
        return 0, "reject", [f"country switched off: {j['country']}"]
    if w is None:
        if j.get("location") and j["location"] != "Not specified" and not any(
                c.lower() in text for c in countries(profile)):
            return 0, "reject", [f"location outside target countries: {j['location']}"]
        pts -= 10
        reasons.append("country unknown")
    else:
        pts = int(pts * (0.85 + 0.15 * w))

    # 7. "Based in X" condition - flag, don't reject (GCC national, short move)
    if re.search(r"\b(must be|currently|only)\s+(based|located|residing)\s+in\b", text):
        pts -= 8
        reasons.append("wants local candidates")

    s = max(0, min(100, pts))
    sp = profile["search"]
    threshold = sp["min_score"] if cat == "civil" else (sp.get("min_score_oilgas", sp["min_score"]) if cat == "oil_gas" else sp.get("min_score_other", sp["min_score"] + 5))
    verdict = "apply" if s >= threshold else ("maybe" if s >= threshold - 15 else "reject")
    return s, verdict, reasons
