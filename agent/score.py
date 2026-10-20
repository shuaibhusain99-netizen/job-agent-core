"""Score a job against profile.json. Transparent rules, 0-100, with the reasons recorded."""
from __future__ import annotations
import re
from datetime import date
from .util import countries, country_weight, years_experience

YEARS_RANGE = re.compile(r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I)
YEARS_MIN = re.compile(r"\b(?:minimum|min\.?|at least|more than|over)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I)
YEARS_PLUS = re.compile(r"\b(\d{1,2})\s*\+\s*(?:years|yrs)", re.I)
YEARS_ANY = re.compile(r"\b(\d{1,2})\s*(?:years|yrs)", re.I)
EXP_WORD = re.compile(r"\bexperience|\bexp\b|\bbackground\b|\bpost[- ]?qualification|\bpost[- ]?graduat", re.I)
# sentences about the employer, not the candidate: "a contractor with over 25 years of experience in the Kingdom"
COMPANY_TALK = re.compile(r"\b(we|our|us|company|firm|group|organi[sz]ation|established|founded|history|heritage|track record|"
                          r"legacy|in business|serving|has (over|more than)|have (over|more than)|since \d{4})\b", re.I)


def required_years(text: str) -> tuple[int | None, int | None]:
    """(min, max) years of experience the posting asks for. Only sentences about the candidate's experience count:
    'a company with 25 years of history', 'a 2-year contract' and 'we have 30 years of experience' are ignored."""
    for sent in re.split(r"[.;\n•●▪]|\s-\s", text):
        if not EXP_WORD.search(sent) or COMPANY_TALK.search(sent):
            continue
        m = YEARS_RANGE.search(sent)
        if m and int(m.group(1)) <= 15:
            return int(m.group(1)), int(m.group(2))
        m = YEARS_MIN.search(sent) or YEARS_PLUS.search(sent) or YEARS_ANY.search(sent)
        if m and int(m.group(1)) <= 15:
            return int(m.group(1)), None
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
    return r"(?<![a-z])" + re.escape(term) + r"(?![a-z])"


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
        if re.search(_word(term), title) and not (term in exempt and re.search(exempt[term], title, re.I)):
            out.append((term, int(pen)))
    return out


def title_only_reject(title: str, profile: dict) -> str | None:
    """Reason a title alone rules a job out (nationals-only, another discipline), or None."""
    t = title.lower()
    for pat in profile.get("title_reject_patterns", []):
        if re.search(pat, t, re.I):
            return f"title rejects: /{pat}/"
    matched = [x for x in _all_targets(profile) if re.search(_word(x), t)]
    heavy = [(term, p) for term, p in _penalties(t, profile, matched) if p <= -50]
    return f"title:{heavy[0][0]}({heavy[0][1]})" if heavy else None


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
    if verdict == "apply" and j["level"] == "senior":
        lo, _ = required_years(text)
        if lo is None or lo > years_experience(profile) + 1:
            verdict = "maybe"                      # senior / lead titles usually want far more years: worth a look, not a send
            reasons.append("senior title - check the years asked")
    wr = (profile.get("work_rights") or {}).get(j.get("country") or "", {})
    if verdict == "apply" and wr.get("visa_needed"):
        no = wr.get("no_sponsor") and re.search(wr["no_sponsor"], text, re.I)
        yes = any(re.search(sig, text, re.I) for sig in wr.get("sponsor_signals", []))
        if no or not yes:
            verdict = "maybe"                      # you need a sponsored work visa there: To apply only when they can sponsor
            reasons.append("needs a work visa - the ad doesn't say they sponsor")
        else:
            reasons.append("may sponsor a visa")
    j["salary"] = find_salary(j["title"] + " " + j.get("description", ""))
    j["apply_email"] = find_apply_email(j.get("description", ""))
    if j["sector"]:
        reasons.append("sector:" + j["sector"])
    if j["salary"]:
        reasons.append("pay: " + j["salary"])
    if j["apply_email"]:
        reasons.append("apply by e-mail")
    return best[0], verdict, reasons


def _score_one(j: dict, profile: dict, cat: str, c: dict) -> tuple[int, str, list[str]]:
    title = j["title"].lower()
    desc = j.get("description", "")
    text = (j["title"] + " " + desc).lower()
    reasons: list[str] = [f"category:{cat}"]
    pts = 0

    # 0. Titles that exclude you outright (nationals-only roles etc.)
    for pat in profile.get("title_reject_patterns", []):
        if re.search(pat, title, re.I):
            return 0, "reject", reasons + [f"title rejects: /{pat}/"]

    # 1. Title match (the strongest signal)
    matched = [t for t in c["target_titles"] if re.search(_word(t), title)]
    if matched:
        pts += 40
        reasons.append("target title")
    elif cat == "civil" and "engineer" in title:
        pts += 15
        reasons.append("engineer title")
    else:
        return 0, "reject", reasons + ["title not in category"]

    # 2. Title penalties (seniority, discipline)
    for term, pen in _penalties(title, profile, matched):
        pts += pen
        reasons.append(f"title:{term}({pen})")

    # 3. Hard rejects anywhere in the text (closed ads, experience far above yours, PhD required, nationals only)
    for pat in profile["hard_reject_patterns"]:
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

    # 5. Experience fit
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

    # 6. Industry checks. Titles that exist in every industry (project engineer, stores, reporting ...) only count fully when
    #    the title, company or ad shows construction / infrastructure work. With only a listing snippet we can't tell yet:
    #    a small penalty, and the full ad (fetched for promising jobs) decides.
    ctx_text = text + " " + j.get("company", "").lower()
    short = len(desc) < 400
    need = c.get("require_any")
    if need and not any(re.search(_word(w), ctx_text) for w in need):
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
        if re.search(_word(term), text):
            kw += p
            hits.append(term)
    pts += min(kw, 40)
    if len(desc) < 400 and matched and j.get("source") not in profile["search"].get("no_listing_bonus_sources", []):
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
        if j.get("location") and j["location"] not in ("Not specified", "") and not any(
                re.search(_word(cn.lower()), text) for cn in countries(profile)):
            return 0, "reject", reasons + [f"location outside target countries: {j['location']}"]
        pts -= 10
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
