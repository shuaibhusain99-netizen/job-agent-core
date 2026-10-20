"""The job list (tracker.csv in the data folder).

Safety rules:
- one writer at a time: a thread lock plus a lock file shared by the app and the scheduled search;
- writes are atomic (temp file + rename), so a crash or power cut never leaves a half-written list;
- your own changes win: when a search saves, status / applied date / notes you changed while it ran are kept;
- cells that Excel would treat as formulas (= + - @ ...) are stored with a leading apostrophe;
- if the list is open in Excel when a search finishes, the results go to tracker.pending.csv and are merged next time;
- a list Excel re-saved (other encoding, ';' separator, dd/mm/yyyy dates) is still read correctly - and a file that
  can't be read at all is never overwritten.
"""
from __future__ import annotations
import csv, shutil, io, os, re, secrets, threading, time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .util import log

FIELDS = ["found_date", "score", "verdict", "title", "company", "location", "country", "source",
          "url", "status", "applied_date", "notes", "pack_file", "user_notes", "category", "sector", "salary", "apply_email",
          "posted", "desc_len", "level", "ai_fit", "ai_summary", "touched", "last_seen"]
USER_FIELDS = ("status", "applied_date", "user_notes", "touched")     # touched = the day you last changed it in the app
DATE_FIELDS = ("found_date", "applied_date", "posted", "touched", "last_seen")
OPEN = ("New", "Review")

LOCK = threading.RLock()


class Unreadable(Exception):
    """The job list exists but can't be read as a job list - it is left exactly as it is."""


# ------------------------------------------------------------------ locking + atomic files
@contextmanager
def locked(path: Path, wait: float = 60):
    """Thread lock + a lock file next to the tracker so the app and the scheduled task never write at the same time."""
    with LOCK:
        lf = Path(str(path) + ".lock")
        end = time.time() + wait
        fd = None
        while fd is None:
            try:
                fd = os.open(lf, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - lf.stat().st_mtime > 300:      # left behind by a crash
                        _break_stale(lf)
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise TimeoutError("the job list is busy - try again in a moment")
                time.sleep(0.2)
        try:
            yield
        finally:
            os.close(fd)
            try:
                lf.unlink()
            except OSError:
                pass


def _break_stale(lf: Path) -> None:
    """Take a crashed writer's lock file away - safely when two programs (the app and the daily task) try at once:
    the file is first renamed to a name only this program knows (only one rename can succeed); if what was taken turns
    out to be fresh (someone else had just re-created it), it is put back."""
    grab = lf.with_name(f"{lf.name}.{os.getpid()}.{secrets.token_hex(3)}.stale")
    try:
        os.rename(lf, grab)
    except OSError:
        return                                      # someone else got there first
    try:
        if time.time() - grab.stat().st_mtime <= 300:
            try:
                os.rename(grab, lf)                 # it was a live lock: give it back (fails if a new one exists)
                return
            except OSError:
                pass
        grab.unlink(missing_ok=True)
    except OSError:
        pass


def atomic_write(path: Path, data, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(3)}.tmp")
    raw = data.encode(encoding) if isinstance(data, str) else data
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        try:
            os.fsync(f.fileno())                      # on disk before it replaces the old file (power cut, sleep)
        except OSError:
            pass
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                     # Windows: the file is open in Excel
            if i == 9:
                tmp.unlink(missing_ok=True)
                raise PermissionError(f"{path.name} is open in another program (Excel?) - close it and try again")
            time.sleep(0.5)


# ------------------------------------------------------------------ CSV with formula protection
FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _esc(v) -> str:
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in FORMULA or (v[:1] == "'" and v[1:2] in FORMULA) else v


def _unesc(v: str) -> str:
    return v[1:] if v[:1] == "'" and (v[1:2] in FORMULA or (v[1:2] == "'" and v[2:3] in FORMULA)) else v


def _s(v) -> str:
    return "" if v is None else str(v)


def norm_date(v: str) -> str:
    """ISO date from what Excel may have written: 2026-10-20, 20/10/2026, 20.10.2026, 2026/10/20. Day first unless
    that is impossible (Bahrain's date order). Anything else is left as it is."""
    v = (v or "").strip()
    if not v or re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    m = re.fullmatch(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", v)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", v)
        if not m:
            return v
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        d, mo = (a, b) if a > 12 or b <= 12 else (b, a)
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return v


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    # re-saved by Excel as "CSV (Comma delimited)": Excel writes this PC's Windows code page (Arabic Windows: cp1256)
    import locale
    for enc in (locale.getpreferredencoding(False), "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("cp1252", errors="replace")


def load(path: Path) -> dict[str, dict]:
    path = Path(path)
    if not path.exists():
        return {}
    text = _decode(path.read_bytes())
    if not text.strip():
        return {}
    head = text.split("\n", 1)[0]
    delim = ";" if head.count(";") > head.count(",") else ("\t" if head.count("\t") > head.count(",") else ",")
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    if "url" not in [(f or "").strip().lstrip("﻿") for f in (reader.fieldnames or [])]:
        raise Unreadable(f"{path.name} can't be read as the job list (was it re-saved in another format?) - it was left unchanged")
    rows = {}
    for r in reader:
        r = {(k or "").strip().lstrip("﻿"): _unesc(v or "") for k, v in r.items() if k}
        for k in DATE_FIELDS:
            if r.get(k):
                r[k] = norm_date(r[k])
        if r.get("url"):
            st = _s(r.get("status")).strip()
            r["status"] = CANON_STATUS.get(st.lower(), st)          # 'applied' typed in Excel is Applied
            old = rows.get(r["url"])
            if old is not None:                                     # the same link twice (a copy-paste in Excel)
                r = _merge_same(old, r)
            r["__orig"] = tuple(r.get(k, "") for k in USER_FIELDS)
            rows[r["url"]] = r
    return rows


CANON_STATUS = {s.lower(): s for s in ("New", "Review", "Applied", "Interview", "Offer", "Rejected", "Skipped", "Found (apply off)")}


def _signal(r: dict) -> tuple:
    return (r.get("status") in ("Applied", "Interview", "Offer", "Rejected"), bool(_s(r.get("applied_date")).strip()),
            bool(_s(r.get("touched")).strip()), bool(_s(r.get("user_notes")).strip()))


def _merge_same(a: dict, b: dict) -> dict:
    """Two rows with one link: keep the one you acted on, and keep both notes."""
    keep, other = (a, b) if _signal(a) >= _signal(b) else (b, a)
    na, nb = _s(keep.get("user_notes")).strip(), _s(other.get("user_notes")).strip()
    if nb and nb not in na:
        keep["user_notes"] = (na + " | " + nb).strip(" |")
    return keep


def to_csv(rows: dict[str, dict], extra: tuple = ()) -> str:
    """The list as CSV text: newest first; within a day, best match first. Columns this version doesn't know (written by
    a newer version) are kept, so going back to an older exe and forward again loses nothing."""
    buf = io.StringIO()
    known = set(FIELDS) | set(extra)
    other = sorted({k for r in rows.values() for k in r if k and k not in known and not k.startswith("_") and not k.startswith("was_")})
    fields = FIELDS + list(extra) + other
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for r in sorted(rows.values(), key=lambda r: (_s(r.get("found_date")), _int(r.get("score"))), reverse=True):
        w.writerow({k: _esc(r.get(k, "")) for k in fields})
    return buf.getvalue()


def write(path: Path, rows: dict[str, dict], extra: tuple = ()) -> None:
    """Write as-is (caller holds the lock). The job list's previous version is kept as tracker.bak.csv."""
    path = Path(path)
    if path.name == "tracker.csv" and path.exists():
        try:
            if path.stat().st_size > 200:
                shutil.copyfile(path, path.with_name("tracker.bak.csv"))
        except OSError:
            pass
    atomic_write(path, "\ufeff" + to_csv(rows, extra))


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def pending_path(path: Path) -> Path:
    return Path(path).with_name("tracker.pending.csv")


WAS = tuple("was_" + k for k in USER_FIELDS)     # pending file: what the list said for your fields when the search saved


def recover_pending(path: Path) -> dict[str, dict] | None:
    """A search that finished while the list was open in Excel left its results in tracker.pending.csv: merge them into
    the list (a change you made since - in the app or in Excel - wins) and write it once the file is free. Returns the
    merged list, also while Excel still has the file open; None when nothing is waiting. Caller holds the lock."""
    path, pend = Path(path), pending_path(path)
    if not pend.exists():
        return None
    try:
        merged = load(pend)
    except Unreadable:
        pend.replace(pend.with_suffix(".unreadable.csv"))
        return None
    for url, d in load(path).items():
        r = merged.get(url)
        if r is None:
            merged[url] = d
            continue
        now = tuple(d.get(k, "") for k in USER_FIELDS)
        if any(r.get(w) for w in WAS) or "was_status" in r:
            mine = now != tuple(r.get(w, "") for w in WAS)          # changed after the search saved
        else:                                           # pending file from an older version: a search never applies or notes
            mine = (_s(d.get("touched")) > _s(r.get("touched"))
                    or (d.get("status") in KEEP_STATUSES and d.get("status") != r.get("status"))
                    or any(_s(d.get(k)) != _s(r.get(k)) for k in ("applied_date", "user_notes")))
        if mine:
            for k in USER_FIELDS:
                r[k] = d.get(k, "")
    for r in merged.values():
        for w in WAS:
            r.pop(w, None)
        r["__orig"] = tuple(r.get(k, "") for k in USER_FIELDS)
    try:
        write(path, merged)
    except PermissionError:
        return merged                                   # still open in Excel: the results keep waiting, nothing is lost
    pend.unlink(missing_ok=True)
    log("Added the results of a search that finished while the job list was open in Excel")
    return merged


def _damaged(path: Path) -> bool:
    """Empty, or cut off in the middle of a line - what a power cut or a sync tool can leave behind."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return False
    return len(raw.strip(b"\xef\xbb\xbf \r\n\t")) == 0 or not raw.endswith(b"\n")


def restore_if_damaged(path: Path) -> bool:
    """A damaged job list is replaced by the last good copy (tracker.bak.csv); the damaged file is kept aside."""
    path = Path(path)
    bak = path.with_name("tracker.bak.csv")
    if not path.exists() or not _damaged(path) or not bak.exists() or _damaged(bak):
        return False
    try:
        good = load(bak)
        if not good:
            return False
        try:
            now = load(path)
        except Unreadable:
            now = {}
        if now and len(now) >= 0.9 * len(good):
            return False                              # no final line break, but complete (another program saved it)
        shutil.copyfile(path, path.with_name(f"tracker.damaged-{int(time.time())}.csv"))
        shutil.copyfile(bak, path)
    except (OSError, Unreadable):
        return False
    log("The job list was damaged (empty or cut off) - restored the last good copy")
    return True


def load_current(path: Path) -> dict[str, dict]:
    """The list as it stands, including results waiting in tracker.pending.csv. Caller holds the lock."""
    restore_if_damaged(path)
    merged = recover_pending(path)
    return merged if merged is not None else load(path)


def save(path: Path, rows: dict[str, dict], dropped=()) -> list[dict]:
    """Save after a search: merge with what is on disk now so changes made in the app during the search are kept.
    `dropped`: rows the search removed on purpose (duplicates). Returns the rows pruned as old."""
    path = Path(path)
    dropped = set(dropped)
    with locked(path):
        disk = load_current(path)
        for url, d in disk.items():
            r = rows.get(url)
            if r is None:
                if url not in dropped or not rejudgeable(d):
                    rows[url] = d                           # added meanwhile, or a duplicate you acted on while it ran
                continue
            now = tuple(d.get(k, "") for k in USER_FIELDS)
            if now != r.get("__orig", now):                 # you changed it while the search ran: yours wins
                for k in USER_FIELDS:
                    r[k] = d.get(k, "")
        old = prune(rows)
        if old:
            log(f"Removed {len(old)} old untouched rows from the job list")
        try:
            write(path, rows)
        except PermissionError:
            for url, r in rows.items():                     # what the list says now, so a later edit in Excel wins
                d = disk.get(url) or {}
                for k, w in zip(USER_FIELDS, WAS):
                    r[w] = d.get(k, "") if d else r.get(k, "")
            write(pending_path(path), rows, extra=WAS)
            for r in rows.values():
                for w in WAS:
                    r.pop(w, None)
            log("The job list is open in another program (Excel?) - today's results were kept and will be added "
                "the next time the app or a search runs")
        return old


# ------------------------------------------------------------------ housekeeping
KEEP_DAYS = 90          # archived rows: kept this long so they aren't re-added as new
OPEN_DAYS = 45          # New / Review rows you never touched and no search has seen for this long: the ad is gone
KEEP_STATUSES = {"Applied", "Interview", "Offer", "Rejected"}


def _date(v):
    try:
        return date.fromisoformat(norm_date(_s(v))[:10])
    except ValueError:
        return None


def prune(rows: dict[str, dict]) -> list[dict]:
    """Drop untouched New/Review rows no search has seen for OPEN_DAYS, and archived rows after KEEP_DAYS.
    Never dropped: anything you applied to, interviewed for, were rejected from, wrote a note on or changed yourself,
    and any row whose date can't be read."""
    today = date.today()

    def stale(r):
        if r.get("status") in KEEP_STATUSES or any(_s(r.get(k)).strip() for k in ("user_notes", "applied_date", "touched")):
            return False
        d = _date(r.get("last_seen") or r.get("found_date"))
        if d is None:
            return False
        return (today - d).days > (OPEN_DAYS if r.get("status") in OPEN else KEEP_DAYS)
    old = [u for u, r in rows.items() if stale(r)]
    return [rows.pop(u) for u in old]


def row_from_job(j: dict) -> dict:
    today = date.today().isoformat()
    return {
        "found_date": today, "last_seen": today, "score": j["match_score"], "verdict": j["verdict"],
        "title": j["title"], "company": j["company"], "location": j["location"], "country": j.get("country", ""),
        "source": j["source"], "url": j["url"],
        "status": {"apply": "New", "found": "Found (apply off)"}.get(j["verdict"], "Review"), "applied_date": "", "notes": j["reasons"][:300],
        "pack_file": j.get("pack_file", ""), "user_notes": "", "category": j.get("category", "civil"),
        "sector": j.get("sector", ""), "salary": j.get("salary", ""), "apply_email": j.get("apply_email", ""),
        "posted": j.get("posted", ""), "desc_len": str(text_len(j)), "level": j.get("level", ""),
        "ai_fit": (j.get("ai") or {}).get("fit", ""), "ai_summary": (j.get("ai") or {}).get("summary", "")[:300],
    }


FILL = ("salary", "apply_email", "posted")


def _untouched(row: dict) -> bool:
    """Open rows you haven't written on or changed yourself."""
    return row.get("status") in OPEN and not (_s(row.get("user_notes")).strip() or _s(row.get("touched")).strip())


def auto_skipped(row: dict) -> bool:
    """Skipped by the agent (not by you): it may be re-judged when the rules or the ad change."""
    return (row.get("status") == "Skipped" and _s(row.get("notes")).startswith("auto:")
            and not (_s(row.get("touched")) or _s(row.get("user_notes")) or _s(row.get("applied_date"))).strip())


def rejudgeable(row: dict) -> bool:
    """Rows the agent may re-score: open rows you haven't touched, and rows it skipped itself."""
    return _untouched(row) or auto_skipped(row)


SAVED_MAX = 8000


def text_len(j: dict) -> int:
    """How much of the ad a job was judged on. A listing teaser (SEEK's bullet points, a Bayt summary) counts as a
    listing however long it is, so the full ad is still read for a match."""
    n = len(j.get("description", ""))
    return min(n, 299) if j.get("_snippet") else n


def _known_len(row: dict) -> int:
    if _s(row.get("desc_len")).strip():
        return _int(row["desc_len"])
    return 0 if "listing only" in _s(row.get("notes")) else 400     # older rows: notes tell us how they were judged


def refresh(row: dict, j: dict, apply_countries: list[str] | None = None, seen: bool = True) -> str:
    """A job already in the list was seen again. Fill in details an older version didn't record and, if you haven't
    acted on it, re-judge it - but only when today's text is at least as complete as what it was judged on before
    (so a listing snippet never overrides a judgement made on the full ad). Returns '', 'updated' or 'skipped'."""
    changed = ""
    if seen:
        row["last_seen"] = date.today().isoformat()
    for k in FILL:
        v = j.get(k, "")
        if v and not row.get(k):
            row[k] = v
            changed = "updated"
    if not rejudgeable(row):
        return changed
    new_len = text_len(j)
    if new_len < min(_known_len(row), SAVED_MAX):        # saved ads are kept to 8,000 characters
        return changed
    if new_len > _known_len(row):
        row["desc_len"] = str(new_len)                  # remember the full ad was read, so it isn't fetched again
        changed = "updated"
    if j["verdict"] == "reject":
        was = row.get("status")
        row.update(status="Skipped", verdict="reject", notes=("auto: " + j["reasons"])[:300], score=j["match_score"],
                   category=j.get("category", row.get("category", "")), sector=j.get("sector", ""), level=j.get("level", row.get("level", "")),
                   desc_len=str(new_len))
        if j.get("ai"):
            row["ai_fit"], row["ai_summary"] = j["ai"].get("fit", ""), j["ai"].get("summary", "")[:300]
        return "skipped" if was != "Skipped" else changed
    status = "New" if j["verdict"] == "apply" else "Review"
    if status == "New" and apply_countries is not None and j.get("country") and j["country"] not in apply_countries:
        status = "Found (apply off)"
    if (_s(row.get("score")), row.get("status"), row.get("category"), row.get("sector")) != \
            (_s(j["match_score"]), status, j.get("category"), j.get("sector", "")):
        row.update(score=j["match_score"], verdict=j["verdict"], status=status, notes=j["reasons"][:300],
                   category=j.get("category", row.get("category")), sector=j.get("sector", ""), desc_len=str(new_len),
                   level=j.get("level", row.get("level", "")), ai_fit=(j.get("ai") or {}).get("fit", row.get("ai_fit", "")),
                   ai_summary=(j.get("ai") or {}).get("summary", row.get("ai_summary", ""))[:300])
        changed = "updated"
    return changed


def _job_from_row(r: dict, description: str = "") -> dict:
    return {"title": r.get("title", ""), "company": r.get("company", ""), "location": r.get("location", ""),
            "country": r.get("country", ""), "description": description, "source": r.get("source", ""),
            "url": r["url"], "posted": r.get("posted", "")}


def reopen_after_rule_changes(rows: dict[str, dict], profile: dict, details_load=None) -> int:
    """Rows the agent skipped only because an older rule set had no place for the title (e.g. 'Civil Inspector' before
    the Site support list existed) are judged again with today's rules, using the saved ad text when there is one."""
    from .score import score, _all_targets, _rx, title_only_reject
    targets = _all_targets(profile)
    n = 0
    for r in rows.values():
        if not auto_skipped(r) or "title not in category" not in _s(r.get("notes")):
            continue
        t = _s(r.get("title")).lower()
        if title_only_reject(t, profile) or not any(_rx(x).search(t) for x in targets):
            continue
        d = (details_load(r["url"]) if details_load else {}) or {}
        if len(d.get("description", "")) < 300 and _known_len(r) >= 400:
            continue                    # it was judged on a full ad we no longer have: that verdict stands
        j = _job_from_row(r, d.get("description", ""))
        s, v, why = score(j, profile)
        if v == "reject":
            r["notes"] = ("auto: " + "; ".join(why))[:300]          # same decision, but now with the real reason
            r["score"] = s
            continue
        r.update(status="Review", verdict=v, score=s, category=j["category"], level=j.get("level", ""), sector=j.get("sector", ""),
                 notes=("re-opened (rules updated): " + "; ".join(why))[:300])
        n += 1
    return n


def release_found(rows: dict[str, dict], apply_countries: list[str]) -> int:
    """Matches kept aside because applying was off for their country: once you switch it on (or the country turns out
    not to be one you switched off), they move to To apply."""
    n = 0
    for r in rows.values():
        if r.get("status") == "Found (apply off)" and not (_s(r.get("touched")) or _s(r.get("user_notes"))).strip() \
                and (not _s(r.get("country")) or r.get("country") in apply_countries):
            r["status"] = "New"
            n += 1
    return n


def _cap_without_ad(r: dict, profile: dict) -> str:
    """Why a job can't be in To apply until its ad has been read: a country where you need a sponsored work visa (only
    the ad can say they sponsor), or a senior title (only the ad says how many years it asks)."""
    if ((profile.get("work_rights") or {}).get(_s(r.get("country"))) or {}).get("visa_needed"):
        return "needs a work visa - the ad doesn't say they sponsor"
    if r.get("level") == "senior":
        return "senior title - check the years asked"
    return ""


def rescore_with_new_rules(rows: dict[str, dict], profile: dict, details_load, apply_countries: list[str]) -> int:
    """After a rules update: rows the agent may re-judge are scored again on their saved ad, so a verdict made under
    old rules doesn't stay forever. Rows judged on an ad that wasn't saved will have it fetched again when next seen."""
    from .score import score
    n = 0
    for r in rows.values():
        if not rejudgeable(r):
            continue
        d = details_load(r["url"]) or {}
        desc = d.get("description", "")
        if len(desc) < 300:
            if _known_len(r) >= 400:
                r["desc_len"] = "0"                    # judged on an ad we don't have: read it again next time it's seen
            cap = _cap_without_ad(r, profile)
            if cap and r.get("status") == "New":       # limits that hold whatever the ad says: To apply waits for the ad
                r.update(status="Review", verdict="maybe", notes=(cap + "; " + _s(r.get("notes")))[:300])
                n += 1
            continue
        j = _job_from_row(r, desc)
        j["match_score"], j["verdict"], why = score(j, profile)
        j["reasons"] = "; ".join(why)
        if refresh(r, j, apply_countries, seen=False):
            n += 1
    return n


def merge_duplicates(rows: dict[str, dict]) -> list[str]:
    """The same job saved twice from different sites (title + company + country + city): keep one row - the one you
    acted on, else the best-judged one. Two different ads on the same site are two openings and both stay.
    Returns the links of the rows removed."""
    from .util import same_job_key, site_key
    groups: dict[str, list] = {}
    for u, r in rows.items():
        k = same_job_key(r)
        if k:
            groups.setdefault(k, []).append(u)
    removed = []
    for urls in groups.values():
        sites = [site_key(u) for u in urls]
        if len(urls) < 2 or len(set(sites)) < 2 or len(set(sites)) != len(urls):
            continue                     # several openings on one site: not one job seen twice
        mine = {u for u in urls if not (rejudgeable(rows[u]) or rows[u].get("status") == "Found (apply off)")}
        keep = max(urls, key=lambda u: (u in mine, rows[u].get("status") in OPEN, _known_len(rows[u]), _int(rows[u].get("score"))))
        for u in urls:
            if u != keep and u not in mine and site_key(u) != site_key(keep):
                rows.pop(u, None)
                removed.append(u)
    return removed


def sweep_titles(rows: dict[str, dict], profile: dict) -> list[dict]:
    """Rules that only need the title (nationals-only, clearly other disciplines) applied to every row you haven't
    acted on. A title penalty doesn't override a judgement made on the full ad. Returns the rows it archived."""
    from .score import title_only_reject
    out = []
    for r in rows.values():
        if not _untouched(r):
            continue
        why = title_only_reject(_s(r.get("title")), profile)
        if why and not (why.startswith("title:") and _known_len(r) >= 400):
            r["status"], r["verdict"], r["notes"] = "Skipped", "reject", ("auto: " + why)[:300]
            r["score"] = "0"                           # what the title rules give it - not the old match
            out.append(r)
    return out


def backfill_from_notes(rows: dict[str, dict]) -> int:
    """Older rows: recover sector / pay from the scoring notes, which recorded them."""
    n = 0
    for r in rows.values():
        notes = _s(r.get("notes"))
        if not r.get("salary"):
            m = re.search(r"pay: ([^;]+)", notes)
            if m:
                r["salary"] = m.group(1).strip(); n += 1
    return n


def retag_old_rows(rows: dict[str, dict], profile: dict) -> int:
    """Rows saved by versions before this one carry category / oil & gas tags from older, broader rules. Recompute
    those display tags once from the title, company and place (statuses, scores and your notes are not touched)."""
    from .score import score
    n = 0
    for r in rows.values():
        if _s(r.get("desc_len")).strip() and _s(r.get("level")).strip():
            continue
        j = _job_from_row(r)
        score(j, profile)
        r["category"], r["sector"], r["level"] = j["category"], j["sector"], j.get("level", "")
        if not _s(r.get("desc_len")).strip():
            r["desc_len"] = str(_known_len(r))
        n += 1
    return n
