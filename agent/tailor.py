"""Template-based tailoring from factual CV blocks. No model, no invented facts.

Produces for each matched job:
  - a 3-4 line CV summary emphasising the themes the posting asks for
  - a short cover letter (3 paragraphs) built from the relevant evidence blocks
  - draft answers to the screening questions found in the posting text
"""
from __future__ import annotations
import re
from datetime import date


def _relevant_blocks(text: str, profile: dict, limit: int = 3) -> list[tuple[str, dict, int]]:
    text = text.lower()
    ranked = []
    for key, block in profile["evidence_blocks"].items():
        if key.startswith("_"):
            continue
        hits = sum(1 for t in block["triggers"] if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", text))
        if hits:
            ranked.append((key, block, hits))
    ranked.sort(key=lambda x: -x[2])
    # always lead with the current role if nothing water-related matched
    if not ranked or ranked[0][0] != "water_networks":
        wb = profile["evidence_blocks"]["water_networks"]
        ranked = [("water_networks", wb, 0)] + [r for r in ranked if r[0] != "water_networks"]
    return ranked[:limit]


def summary(j: dict, profile: dict) -> str:
    c = profile["candidate"]
    themes = [k.replace("_", " ") for k, _, _ in _relevant_blocks(j["title"] + " " + j["description"], profile, 3)]
    theme_txt = {"water networks": "water-network maintenance", "boq contracts": "BOQ and contract administration",
                 "qaqc": "QA/QC and inspection", "roads": "roads and infrastructure", "reporting": "operational reporting",
                 "hse": "HSE", "coordination": "client and subcontractor coordination", "software": "AutoCAD/Civil 3D"}
    focus = ", ".join(theme_txt.get(t, t) for t in themes)
    return (f"Civil Engineer with {c['years_experience']}+ years of site and contract experience in Bahrain, focused on {focus}. "
            f"Currently {c['current_role']}. NEBOSH IGC certified; {c['degree']}. "
            f"{c['nationality']}, open to relocation across the GCC.")


def cover_letter(j: dict, profile: dict) -> str:
    c = profile["candidate"]
    blocks = _relevant_blocks(j["title"] + " " + j["description"], profile, 3)
    body = " ".join(b["text"] for _, b, _ in blocks)
    company = j["company"] if j["company"] != "Unknown" else "your company"
    loc = j["location"] if j["location"] != "Not specified" else (j.get("country") or "the GCC")
    return (
        f"{date.today():%d %B %Y}\n\n"
        f"Dear Hiring Manager,\n\n"
        f"I am applying for the {j['title']} position with {company} in {loc}. I am a Bahraini civil engineer with "
        f"{c['years_experience']} years of site and contract experience on water-network, roads and airport infrastructure, "
        f"and I hold the NEBOSH International General Certificate.\n\n"
        f"{body}\n\n"
        f"{_authorisation_line(j, c)} My notice period is {c['notice_period']}. I would welcome the chance to "
        f"discuss how my experience fits your project.\n\n"
        f"Yours sincerely,\n{c['name']}\n{c['phone']} | {c['email']}"
    )


GCC = {"Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Oman", "Bahrain"}


def _authorisation_line(j: dict, c: dict) -> str:
    country = j.get("country") or "the GCC"
    if country in GCC or country == "the GCC":
        return (f"As a GCC national I can take up a position in {country} without visa sponsorship and am eligible "
                f"for Saudi Council of Engineers registration.")
    return (f"I am a Bahraini national and would need employer-supported work authorisation for {country}; "
            f"I am ready to relocate and to pursue the local engineering licensing route (EIT/P.Eng.) on arrival.")


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
