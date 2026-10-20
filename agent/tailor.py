"""Template-based tailoring from factual CV blocks. No model, no invented facts.

Produces for each matched job:
  - a 3-4 line CV summary emphasising the themes the posting asks for
  - a short cover letter (3 paragraphs) built from the relevant evidence blocks
  - draft answers to the screening questions found in the posting text
"""
from __future__ import annotations
import re
from datetime import date
from .util import years_experience


# Per role family: how the letter introduces you, which evidence leads, and the CV-summary headline.
ROLE = {
    "civil":          ("a civil engineer with {y} years of site and contract experience on water-network, roads and airport infrastructure",
                       "water_networks", "Civil Engineer"),
    "oil_gas":        ("a civil engineer with {y} years of site, QA/QC and HSE experience on live utility, airport and industrial sites",
                       "qaqc", "Civil Engineer"),
    "hse":            ("a NEBOSH IGC-certified civil engineer with {y} years on live construction and utility sites, where I run risk assessments and permit-to-work control",
                       "hse", "NEBOSH-certified Civil / Site Engineer"),
    "qs_planning":    ("a civil engineer with {y} years of contract experience, preparing the monthly Bill of Quantities, quantity reports and progress records on maintenance and infrastructure contracts",
                       "boq_contracts", "Civil Engineer (BOQ, quantities and contract administration)"),
    "stores_supply":  ("a civil engineer with {y} years of site experience who plans and controls the materials, equipment and stock for a water-network maintenance contract",
                       "inventory_materials", "Civil Engineer (materials, inventory and site logistics)"),
    "data_reporting": ("a civil engineer with {y} years of experience who produces the daily, monthly and KPI performance reports for a national water utility",
                       "reporting", "Civil Engineer (operations reporting and KPIs)"),
}


def _role(j: dict) -> tuple[str, str, str]:
    return ROLE.get(j.get("category") or "civil", ROLE["civil"])


def _relevant_blocks(text: str, profile: dict, limit: int = 3, lead: str = "water_networks") -> list[tuple[str, dict, int]]:
    text = text.lower()
    ranked = []
    for key, block in profile["evidence_blocks"].items():
        if key.startswith("_"):
            continue
        hits = sum(1 for t in block["triggers"] if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", text))
        if hits:
            ranked.append((key, block, hits))
    ranked.sort(key=lambda x: -x[2])
    # the block that fits this kind of role always leads; the current water-network job always appears
    eb = profile["evidence_blocks"]
    order = [lead] + (["water_networks"] if lead != "water_networks" else [])
    for key in reversed(order):
        if key in eb:
            ranked = [(key, eb[key], 0)] + [r for r in ranked if r[0] != key]
    return ranked[:limit]


def summary(j: dict, profile: dict) -> str:
    c = profile["candidate"]
    intro, lead, headline = _role(j)
    themes = [k.replace("_", " ") for k, _, _ in _relevant_blocks(j["title"] + " " + j["description"], profile, 3, lead)]
    theme_txt = {"water networks": "water-network maintenance", "boq contracts": "BOQ and contract administration",
                 "qaqc": "QA/QC and inspection", "roads": "roads and infrastructure", "reporting": "operational reporting",
                 "hse": "HSE", "coordination": "client and subcontractor coordination", "software": "AutoCAD/Civil 3D",
                 "inventory materials": "materials and inventory control"}
    focus = ", ".join(theme_txt.get(t, t) for t in themes)
    return (f"{headline} with {years_experience(profile)}+ years of site and contract experience in Bahrain, focused on {focus}. "
            f"Currently {c['current_role']}. NEBOSH IGC certified; {c['degree']}. "
            f"Open to relocation.")


def cover_letter(j: dict, profile: dict) -> str:
    c = profile["candidate"]
    intro, lead, _ = _role(j)
    blocks = _relevant_blocks(j["title"] + " " + j["description"], profile, 3, lead)
    body = " ".join(b["text"] for _, b, _ in blocks)
    company = _company(j)
    loc = _place(j)
    nebosh = "" if j.get("category") in ("hse",) or any(k == "hse" for k, _, _ in blocks) else ", and I hold the NEBOSH International General Certificate"
    return (
        f"{date.today():%d %B %Y}\n\n"
        f"Dear Hiring Manager,\n\n"
        f"I am applying for the {j['title']} position with {company}{(' in ' + loc) if loc else ''}. "
        f"I am {intro.format(y=years_experience(profile))}{nebosh}.\n\n"
        f"{body}\n\n"
        f"{_authorisation_line(j, c)} My notice period is {c['notice_period']}. I would welcome the chance to "
        f"discuss how my experience fits your project.\n\n"
        f"Yours sincerely,\n{c['name']}\n{c['phone']} | {c['email']}"
    )


GCC = {"Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Oman", "Bahrain"}


THE = {"United Arab Emirates", "GCC", "United States", "United Kingdom"}
SOURCES = {"unknown", "", "gulf job paper", "linkedin", "seek", "rigzone", "jooble", "kuwait jobs here"}


def _country_name(c: str) -> str:
    return f"the {c}" if c in THE else c


def _company(j: dict) -> str:
    c = (j.get("company") or "").strip()
    return "your company" if c.lower() in SOURCES or len(c) < 2 else c


def _place(j: dict) -> str:
    """'Riyadh, Saudi Arabia' from whatever the source gave ('AE - Dubai', 'Riyadh, Riyadh, Saudi Arabia', 'Not specified')."""
    loc = re.sub(r"^[A-Z]{2,3}\s*-+\s*", "", (j.get("location") or "").strip())
    parts = []
    for p in (x.strip() for x in loc.split(",")):
        if p and p.lower() not in ("not specified", "remote", "other") and p.lower() not in (x.lower() for x in parts):
            parts.append(p)
    country = j.get("country") or ""
    if country and country != "Other" and not any(country.lower() in p.lower() for p in parts):
        parts.append(country)
    text = ", ".join(parts[-2:]) if len(parts) > 2 else ", ".join(parts)
    return re.sub(r"\bUnited Arab Emirates\b", "the United Arab Emirates", text) if text == "United Arab Emirates" else text


def _authorisation_line(j: dict, c: dict) -> str:
    country = j.get("country") or ""
    return f"I am ready to relocate to {_country_name(country)}." if country and country != "Other" else "I am ready to relocate."


QUESTION_RE = re.compile(r"([A-Z][^.?!]{15,160}\?)")


def screening_answers(j: dict, profile: dict) -> list[tuple[str, str]]:
    """Find question-looking sentences in the posting and pair them with a factual answer."""
    out = []
    for q in QUESTION_RE.findall(j["description"])[:8]:
        ans = answer_for(q, profile)
        if ans:
            out.append((q.strip(), ans))
    return out


def answer_for(question: str, profile: dict) -> str | None:
    for pat, ans in profile["screening_answers"].items():
        if pat.startswith("_"):
            continue
        if re.search(pat, question, re.I):
            return ans
    return None


def application_pack(j: dict, profile: dict) -> str:
    qa = screening_answers(j, profile)
    lines = [
        f"# {j['title']} — {j['company']}",
        f"Location: {j['location']} ({j.get('country') or 'country n/a'})  ",
        f"Source: {j['source']}  ",
        f"Link: {j['url']}  ",
        f"Score: {j['match_score']} ({j['verdict']}) — {j['reasons']}",
        "",
        "## CV summary for this application",
        summary(j, profile),
        "",
        "## Cover letter",
        cover_letter(j, profile),
        "",
        "## Draft screening answers",
    ]
    if qa:
        for q, a in qa:
            lines += [f"**Q:** {q}", f"**A:** {a}", ""]
    else:
        lines.append("_No explicit questions found in the posting. Standard answers are in profile.json → screening_answers._")
    return "\n".join(lines)
