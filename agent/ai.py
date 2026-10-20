"""AI reviewer: reads each promising ad the way a recruiter would and judges it against the candidate.

Works with any OpenAI-compatible chat API:
  - Groq (free account, no card): base_url https://api.groq.com/openai/v1, model e.g. meta-llama/llama-4-scout-17b-16e-instruct
  - Ollama on the same PC (free, fully private): base_url http://127.0.0.1:11434/v1, model e.g. qwen2.5:7b
Settings live in keys.json -> "ai". Off unless a provider is configured.

What is sent: the ad text and a profile summary (degree, experience, skills, certificates, target roles and countries,
GCC-national work status). Never the name, phone, e-mail or address.
"""
from __future__ import annotations
import hashlib, json, re, threading, time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .util import log, years_experience

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "meta-llama/llama-4-scout-17b-16e-instruct", "rpm": 25},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b", "rpm": 600},
}
ROLE_FAMILIES = ["civil", "oil_gas", "hse", "qs_planning", "stores_supply", "data_reporting", "site_support", "other"]
LEVELS = ["starter", "support", "match", "senior"]
USAGE: Path = Path("ai_usage.json")       # set by main / app
_lock = threading.Lock()
_last = [0.0]


def settings(keys: dict) -> dict | None:
    """Usable AI settings from keys.json, or None when the reviewer is off / not configured."""
    a = (keys or {}).get("ai") or {}
    prov = a.get("provider") or ""
    if not a.get("enabled") or prov not in PROVIDERS:
        return None
    s = dict(PROVIDERS[prov])
    s.update({k: v for k, v in a.items() if v not in (None, "") and k in ("base_url", "model", "rpm", "api_key", "max_per_run", "daily_cap")})
    if prov == "groq" and not s.get("api_key"):
        return None
    s["provider"] = prov
    s.setdefault("max_per_run", 150)
    s.setdefault("daily_cap", 900)
    return s


# ------------------------------------------------------------------ transport
def _pace(rpm: int):
    with _lock:
        gap = 60.0 / max(1, int(rpm))
        wait = _last[0] + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()


def _usage(add: int = 0) -> int:
    try:
        u = json.loads(USAGE.read_text(encoding="utf-8"))
    except Exception:
        u = {}
    if u.get("date") != date.today().isoformat():
        u = {"date": date.today().isoformat(), "count": 0}
    if add:
        u["count"] += add
        try:
            USAGE.write_text(json.dumps(u), encoding="utf-8")
        except OSError:
            pass
    return u["count"]


def chat(s: dict, system: str, user: str, json_mode: bool = True, max_tokens: int = 700, timeout: int = 60) -> str:
    """One chat completion. Retries politely on rate limits. Raises on hard errors."""
    body = {"model": s["model"], "temperature": 0.1, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json", "User-Agent": "JobAgent/1.0"}
    if s.get("api_key"):
        headers["Authorization"] = f"Bearer {s['api_key']}"
    url = s["base_url"].rstrip("/") + "/chat/completions"
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise ValueError("AI address must be https (or this PC)")
    for attempt in range(4):
        _pace(s.get("rpm", 25))
        try:
            with urlopen(Request(url, data=json.dumps(body).encode(), headers=headers, method="POST"), timeout=timeout) as r:
                data = json.loads(r.read(2_000_000))
            _usage(1)
            return data["choices"][0]["message"]["content"] or ""
        except HTTPError as e:
            if e.code == 429 and attempt < 3:
                try:
                    wait = float(e.headers.get("retry-after") or 0)
                except ValueError:
                    wait = 0
                time.sleep(min(60, max(wait, 5 * (attempt + 1))))
                continue
            if e.code == 400 and json_mode and attempt == 0:       # model without JSON mode: ask in the prompt instead
                body.pop("response_format", None)
                continue
            detail = e.read(300).decode("utf-8", "replace")
            raise RuntimeError(f"AI HTTP {e.code}: {detail}") from None
        except (URLError, TimeoutError, OSError) as e:
            if attempt < 2:
                time.sleep(3)
                continue
            raise RuntimeError(f"AI not reachable: {e}") from None
    raise RuntimeError("AI rate limit - try again later")


def _json(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        raise ValueError("no JSON in AI reply")
    return json.loads(m.group(0))


def test(s: dict) -> str:
    out = chat(s, "Reply with JSON only.", 'Return {"ok": true}.', max_tokens=20)
    return "ok" if _json(out).get("ok") else out[:80]


# ------------------------------------------------------------------ the review
def profile_brief(profile: dict) -> str:
    """What the reviewer knows about the candidate - facts only, no contact details."""
    c = profile["candidate"]
    ev = profile.get("evidence_blocks", {})
    exp = " ".join(b["text"] for k, b in ev.items() if not k.startswith("_") and k in ("water_networks", "boq_contracts", "qaqc", "roads", "hse", "inventory_materials", "reporting"))
    countries = [k for k, v in profile["search"]["countries"].items() if not k.startswith("_") and isinstance(v, dict) and v.get("search")]
    return (
        f"Degree: {c.get('degree')}. Experience: {years_experience(profile)} years (since {c.get('experience_start')}). "
        f"Current role: {c.get('current_role')}. Certifications: {', '.join(c.get('certifications', []))}. "
        f"Software: {', '.join(c.get('software', []))}. Languages: {', '.join(c.get('languages', []))}. "
        f"Work status: {profile.get('ai_facts', {}).get('work_status', 'GCC national (Bahrain).')} "
        f"Notice period: {c.get('notice_period')}. Target countries: {', '.join(countries)}.\n"
        f"Experience details: {exp[:2500]}\n"
        f"Wants: civil / site / project / QA-QC / water & roads engineering roles first; also HSE, quantity surveying, planning, "
        f"document control, stores / materials / inventory, operations reporting, and oil & gas roles. Junior, graduate, "
        f"trainee, technician, inspector, supervisor and foreman roles are ALL acceptable (he is happy to start below his level).")


SYSTEM = (
    "You are an experienced Gulf construction recruiter screening job ads for ONE candidate. Judge realistically whether he "
    "could get an interview. Rules: (1) Below-level roles (junior, graduate, trainee, technician, supervisor, inspector, foreman, "
    "assistant) are GOOD fits for him, not bad. (2) Deal-breakers are only hard requirements he cannot meet: a specific nationality "
    "that is not his (e.g. 'Saudi nationals only'; 'GCC nationals' is fine for him), clearly more years than he has (more than his "
    "years + 2 as a minimum), a different engineering discipline required (electrical, mechanical, chemical...), a licence he cannot "
    "get, citizenship / permanent residence / security clearance, or a location outside his target countries. Company boilerplate "
    "('we have 30 years of experience') is NOT a requirement. (3) Use only facts in the ad and the profile; never invent. "
    "Reply with JSON only, exactly these keys: "
    '{"fit": 0-100, "decision": "apply"|"maybe"|"skip", "level": "starter"|"support"|"match"|"senior", '
    '"role_family": "civil"|"oil_gas"|"hse"|"qs_planning"|"stores_supply"|"data_reporting"|"site_support"|"other", '
    '"deal_breakers": [short strings], "strengths": [up to 3 short strings], "gaps": [up to 3 short strings], '
    '"summary": "one sentence a busy person reads", "years_required": number or null}. '
    "level: starter = fresh/graduate/trainee/junior/0-2 years; support = technician/inspector/supervisor/foreman/draftsman; "
    "match = engineer roles asking roughly his experience; senior = lead/senior/manager or clearly more experience. "
    "role_family: site_support for technician/inspector/supervisor/foreman/draftsman roles in construction or utilities. "
    "fit: 85+ strong match he should apply to today; 60-84 good; 40-59 possible; below 40 poor.")


def review(job: dict, profile: dict, s: dict) -> dict:
    ad = (job.get("description") or "")[:3500]
    user = (f"CANDIDATE:\n{profile_brief(profile)}\n\nJOB AD:\nTitle: {job['title']}\nCompany: {job['company']}\n"
            f"Location: {job.get('location')} ({job.get('country') or 'country unknown'})\nPosted: {job.get('posted') or 'unknown'}\n"
            f"Text: {ad}")
    out = _json(chat(s, SYSTEM, user))
    r = {
        "fit": max(0, min(100, int(float(out.get("fit") or 0)))),
        "decision": out.get("decision") if out.get("decision") in ("apply", "maybe", "skip") else "maybe",
        "level": out.get("level") if out.get("level") in LEVELS else "",
        "role_family": out.get("role_family") if out.get("role_family") in ROLE_FAMILIES else "",
        "deal_breakers": [str(x)[:120] for x in (out.get("deal_breakers") or [])][:4],
        "strengths": [str(x)[:120] for x in (out.get("strengths") or [])][:3],
        "gaps": [str(x)[:120] for x in (out.get("gaps") or [])][:3],
        "summary": str(out.get("summary") or "")[:300],
        "years_required": out.get("years_required") if isinstance(out.get("years_required"), (int, float)) else None,
        "model": s["model"], "on": date.today().isoformat(),
        "ad_hash": hashlib.sha1(ad.encode("utf-8")).hexdigest()[:12],
    }
    return r


def combine(rule_score: int, rule_verdict: str, r: dict, threshold: int) -> tuple[int, str]:
    """Final score: the AI's reading counts most; hard deal-breakers reject."""
    if r["decision"] == "skip" and r["deal_breakers"]:
        return min(rule_score, r["fit"]), "reject"
    score = round(0.35 * rule_score + 0.65 * r["fit"])
    if r["decision"] == "apply" and r["fit"] >= 70:
        score = max(score, threshold)
    verdict = "apply" if score >= threshold else ("maybe" if score >= threshold - 15 else "reject")
    return score, verdict


LETTER_SYSTEM = (
    "You write short, specific cover letters for a civil engineer. Use ONLY facts from the CANDIDATE section - never invent "
    "projects, employers, numbers, certificates or skills. Mirror 2-3 needs from the ad and show the matching evidence. "
    "Plain, confident English; no clichés; 170-230 words; 3 short paragraphs; start with 'Dear Hiring Manager,' and end with "
    "'Yours sincerely,' followed by the name line given. No date, no address block, no placeholders.")


def letter(job: dict, profile: dict, s: dict) -> str:
    c = profile["candidate"]
    facts = profile_brief(profile) + "\nEvidence (use these sentences' facts only):\n" + "\n".join(
        f"- {b['text']}" for k, b in profile.get("evidence_blocks", {}).items() if not k.startswith("_"))
    user = (f"CANDIDATE:\n{facts}\nName line: {c['name']}\n\nJOB AD:\nTitle: {job['title']}\nCompany: {job['company']}\n"
            f"Location: {job.get('location')}\nText: {(job.get('description') or '')[:3500]}")
    out = chat(s, LETTER_SYSTEM, user, json_mode=False, max_tokens=600).strip()
    return out + f"\n{c['phone']} | {c['email']}"


def review_many(jobs: list[dict], profile: dict, keys: dict, cache_get, cache_put, stop=None) -> int:
    """Review jobs in order (best first) within the run and daily limits. cache_get(url)->dict, cache_put(url, review)."""
    s = settings(keys)
    if not s:
        return 0
    left = min(int(s["max_per_run"]), max(0, int(s["daily_cap"]) - _usage()))
    done = errors = 0
    for j in jobs:
        if left <= 0 or (stop and stop()):
            break
        ad = (j.get("description") or "")[:3500]
        if len(ad) < 150:
            continue
        old = cache_get(j["url"])
        if old and old.get("ad_hash") == hashlib.sha1(ad.encode("utf-8")).hexdigest()[:12]:
            j["ai"] = old
            continue
        try:
            j["ai"] = review(j, profile, s)
            cache_put(j["url"], j["ai"])
            done += 1
            left -= 1
        except Exception as e:
            errors += 1
            log(f"  AI review skipped ({str(e)[:90]})")
            if errors >= 5:
                log("AI reviewer: stopping for this run after repeated errors")
                break
    log(f"AI reviewer ({s['provider']}, {s['model']}): read {done} ads" + (f", {errors} errors" if errors else ""))
    return done
