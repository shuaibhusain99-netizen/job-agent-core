"""The job list (tracker.csv in the data folder).

Safety rules:
- one writer at a time: a thread lock plus a lock file shared by the app and the scheduled search;
- writes are atomic (temp file + rename), so a crash or power cut never leaves a half-written list;
- your own changes win: when a search saves, status / applied date / notes you changed while it ran are kept;
- cells that Excel would treat as formulas (= + - @ ...) are stored with a leading apostrophe.
"""
from __future__ import annotations
import csv, io, os, threading, time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from .util import log

FIELDS = ["found_date", "score", "verdict", "title", "company", "location", "country", "source",
          "url", "status", "applied_date", "notes", "pack_file", "user_notes", "category", "sector", "salary", "apply_email",
          "posted", "desc_len", "level", "ai_fit", "ai_summary"]
USER_FIELDS = ("status", "applied_date", "user_notes")
OPEN = ("New", "Review")

LOCK = threading.RLock()


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
                        lf.unlink()
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


def atomic_write(path: Path, data, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding=encoding, newline="")
    else:
        tmp.write_bytes(data)
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
    return "'" + v if v[:1] in FORMULA else v


def _unesc(v: str) -> str:
    return v[1:] if v[:1] == "'" and v[1:2] in FORMULA else v


def load(path: Path) -> dict[str, dict]:
    path = Path(path)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    rows = {}
    for r in csv.DictReader(io.StringIO(text)):
        r = {k: _unesc(v or "") for k, v in r.items() if k}
        if r.get("url"):
            r["__orig"] = tuple(r.get(k, "") for k in USER_FIELDS)
            rows[r["url"]] = r
    return rows


def write(path: Path, rows: dict[str, dict]) -> None:
    """Write as-is (caller holds the lock)."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for r in sorted(rows.values(), key=lambda r: (r.get("found_date", ""), -_int(r.get("score"))), reverse=True):
        w.writerow({k: _esc(r.get(k, "")) for k in FIELDS})
    atomic_write(path, "﻿" + buf.getvalue())


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def save(path: Path, rows: dict[str, dict]) -> None:
    """Save after a search: merge with what is on disk now so changes made in the app during the search are kept."""
    path = Path(path)
    with locked(path):
        disk = load(path)
        for url, d in disk.items():
            r = rows.get(url)
            if r is None:
                rows[url] = d                                   # added meanwhile (shouldn't happen, but never lose a row)
                continue
            now = tuple(d.get(k, "") for k in USER_FIELDS)
            if now != r.get("__orig", now):                     # you changed it while the search ran: yours wins
                for k in USER_FIELDS:
                    r[k] = d.get(k, "")
        n = prune(rows)
        if n:
            log(f"Removed {n} old untouched rows from the job list")
        write(path, rows)


# ------------------------------------------------------------------ housekeeping
KEEP_DAYS = 90          # archived rows: kept this long so they aren't re-added as new
OPEN_DAYS = 45          # New / Review rows you never touched: postings close long before this
KEEP_STATUSES = {"Applied", "Interview", "Offer", "Rejected"}


def prune(rows: dict[str, dict]) -> int:
    """Drop untouched New/Review rows after OPEN_DAYS and archived rows after KEEP_DAYS.
    Never dropped: anything you applied to, interviewed for, were rejected from, or wrote a note on."""
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    open_cut = (date.today() - timedelta(days=OPEN_DAYS)).isoformat()

    def stale(r):
        if r.get("status") in KEEP_STATUSES or (r.get("user_notes") or "").strip() or (r.get("applied_date") or "").strip():
            return False
        d = r.get("found_date") or ""
        if not d:
            return False
        return d < (open_cut if r.get("status") in OPEN else cutoff)
    old = [u for u, r in rows.items() if stale(r)]
    for u in old:
        rows.pop(u, None)
    return len(old)


def row_from_job(j: dict) -> dict:
    return {
        "found_date": date.today().isoformat(), "score": j["match_score"], "verdict": j["verdict"],
        "title": j["title"], "company": j["company"], "location": j["location"], "country": j.get("country", ""),
        "source": j["source"], "url": j["url"],
        "status": {"apply": "New", "found": "Found (apply off)"}.get(j["verdict"], "Review"), "applied_date": "", "notes": j["reasons"][:200],
        "pack_file": j.get("pack_file", ""), "user_notes": "", "category": j.get("category", "civil"),
        "sector": j.get("sector", ""), "salary": j.get("salary", ""), "apply_email": j.get("apply_email", ""),
        "posted": j.get("posted", ""), "desc_len": len(j.get("description", "")), "level": j.get("level", ""),
        "ai_fit": (j.get("ai") or {}).get("fit", ""), "ai_summary": (j.get("ai") or {}).get("summary", "")[:200],
    }


FILL = ("salary", "apply_email", "posted")


def _untouched(row: dict) -> bool:
    return row.get("status") in OPEN and not (row.get("user_notes") or "").strip()


def _known_len(row: dict) -> int:
    if (row.get("desc_len") or "").strip():
        return _int(row["desc_len"])
    return 0 if "listing only" in (row.get("notes") or "") else 400     # older rows: notes tell us how they were judged


def refresh(row: dict, j: dict, apply_countries: list[str] | None = None) -> str:
    """A job already in the list was seen again. Fill in details an older version didn't record and, if you haven't
    acted on it, re-judge it - but only when today's text is at least as complete as what it was judged on before
    (so a listing snippet never overrides a judgement made on the full ad). Returns '', 'updated' or 'skipped'."""
    changed = ""
    for k in FILL:
        v = j.get(k, "")
        if v and not row.get(k):
            row[k] = v
            changed = "updated"
    if not _untouched(row):
        return changed
    new_len = len(j.get("description", ""))
    if new_len < _known_len(row):
        return changed
    if j["verdict"] == "reject":
        row["status"], row["verdict"], row["notes"] = "Skipped", "reject", ("auto: " + j["reasons"])[:200]
        row["desc_len"] = new_len
        if j.get("ai"):
            row["ai_fit"], row["ai_summary"] = j["ai"].get("fit", ""), j["ai"].get("summary", "")[:200]
        return "skipped"
    status = "New" if j["verdict"] == "apply" else "Review"
    if status == "New" and apply_countries is not None and j.get("country") not in apply_countries:
        status = "Found (apply off)"
    if (str(row.get("score")), row.get("status"), row.get("category"), row.get("sector")) != \
            (str(j["match_score"]), status, j.get("category"), j.get("sector", "")):
        row.update(score=j["match_score"], verdict=j["verdict"], status=status, notes=j["reasons"][:200],
                   category=j.get("category", row.get("category")), sector=j.get("sector", ""), desc_len=new_len,
                   level=j.get("level", row.get("level", "")), ai_fit=(j.get("ai") or {}).get("fit", row.get("ai_fit", "")),
                   ai_summary=(j.get("ai") or {}).get("summary", row.get("ai_summary", ""))[:200])
        changed = "updated"
    return changed


def sweep_titles(rows: dict[str, dict], profile: dict) -> list[dict]:
    """Rules that only need the title (nationals-only, clearly other disciplines) applied to every row you haven't
    acted on. Returns the rows it archived."""
    from .score import title_only_reject
    out = []
    for r in rows.values():
        if not _untouched(r):
            continue
        why = title_only_reject(r.get("title") or "", profile)
        if why:
            r["status"], r["verdict"], r["notes"] = "Skipped", "reject", ("auto: " + why)[:200]
            out.append(r)
    return out


def backfill_from_notes(rows: dict[str, dict]) -> int:
    """Older rows: recover sector / pay from the scoring notes, which recorded them."""
    import re
    n = 0
    for r in rows.values():
        notes = r.get("notes", "")
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
        if (r.get("desc_len") or "").strip() and (r.get("level") or "").strip():
            continue
        j = {"title": r.get("title", ""), "company": r.get("company", ""), "location": r.get("location", ""),
             "country": r.get("country", ""), "description": "", "source": r.get("source", ""), "url": r.get("url", "")}
        score(j, profile)
        r["category"], r["sector"], r["level"] = j["category"], j["sector"], j.get("level", "")
        if not (r.get("desc_len") or "").strip():
            r["desc_len"] = str(_known_len(r))
        n += 1
    return n
