"""Score a job against profile.json. Transparent rules, 0-100, with the reasons recorded."""
from __future__ import annotations
import re
from .util import countries, country_weight

YEARS_RANGE = re.compile(r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I)
YEARS_MIN = re.compile(r"\b(?:minimum|min\.?|at least|more than|over)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:years|yrs)", re.I)
YEARS_PLUS = re.compile(r"\b(\d{1,2})\s*\+\s*(?:years|yrs)", re.I)
YEARS_ANY = re.compile(r"\b(\d{1,2})\s*(?:years|yrs)", re.I)


def required_years(text: str) -> tuple[int | None, int | None]:
    """Return (min, max) years asked for, if the text states them."""
    m = YEARS_RANGE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = YEARS_MIN.search(text) or YEARS_PLUS.search(text)
    if m:
        return int(m.group(1)), None
    m = YEARS_ANY.search(text)
    if m:
        return int(m.group(1)), None
    return None, None


def score(j: dict, profile: dict) -> tuple[int, str, list[str]]:
    """Returns (score, verdict, reasons). verdict: 'apply' | 'maybe' | 'reject'."""
    title = j["title"].lower()
    text = (j["title"] + " " + j["description"]).lower()
    reasons: list[str] = []
    pts = 0

    # 1. Title match (the strongest signal)
    if any(t in title for t in profile["target_titles"]):
        pts += 40
        reasons.append("target title")
    elif "engineer" in title:
        pts += 15
        reasons.append("engineer title")
    else:
        pts -= 20
        reasons.append("non-engineer title")

    # 2. Title penalties (seniority, discipline, nationality restrictions)
    for term, pen in profile["title_penalties"].items():
        if term in title:
            pts += pen
            reasons.append(f"title:{term}({pen})")

    # 3. Hard rejects anywhere in the text
    for pat in profile["hard_reject_patterns"]:
        if re.search(pat, text, re.I):
            return 0, "reject", [f"hard reject: /{pat}/"]

    # 4. Experience fit
    yrs = profile["candidate"]["years_experience"]
    lo, hi = required_years(text)
    if lo is not None:
        if lo <= yrs and (hi is None or hi >= yrs):
            pts += 15
            reasons.append(f"experience fits ({lo}-{hi or '+'} yrs)")
        elif lo <= yrs + 1:
            pts += 3
            reasons.append(f"experience close ({lo}+ yrs)")
        else:
            pts -= 30
            reasons.append(f"asks {lo}+ yrs")

    # 5. Keyword points (capped so a long description can't dominate)
    kw = 0
    hits = []
    for term, p in profile["keyword_points"].items():
        if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text):
            kw += p
            hits.append(term)
    kw = min(kw, 40)
    pts += kw
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
    verdict = "apply" if s >= profile["search"]["min_score"] else ("maybe" if s >= profile["search"]["min_score"] - 15 else "reject")
    return s, verdict, reasons
