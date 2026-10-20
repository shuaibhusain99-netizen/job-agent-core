"""Per-job details kept next to the job list: the ad text, and the AI reviewer's verdict.

data/details/<sha1 of url>.json - small JSON files, written atomically, pruned with the job list.
The job list (tracker.csv) stays a compact table; the app's detail panel reads these files.
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path

ROOT: Path = Path("details")       # set by main / app to <data>/details


def key(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()


def path(url: str) -> Path:
    return ROOT / f"{key(url)}.json"


def load(url: str) -> dict:
    try:
        return json.loads(path(url).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(url: str, data: dict) -> None:
    from .tracker import atomic_write
    ROOT.mkdir(parents=True, exist_ok=True)
    old = load(url)
    old.update({k: v for k, v in data.items() if v not in (None, "")})
    old["url"] = url
    atomic_write(path(url), json.dumps(old, ensure_ascii=False))


def prune(keep_urls, recent_days: int = 30) -> int:
    """Delete details of jobs no longer in the list - except, for a month, an AI review or a note that the ad couldn't be
    read (so a job the AI already judged, or a page that keeps failing, isn't asked about again every day)."""
    import time
    if not ROOT.exists():
        return 0
    keep = {key(u) for u in keep_urls}
    cut = time.time() - recent_days * 86400
    n = 0
    for f in ROOT.glob("*.json"):
        if f.stem in keep:
            continue
        try:
            if f.stat().st_mtime > cut:
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("ai") or d.get("tried"):
                    continue
            f.unlink(missing_ok=True)
            n += 1
        except (OSError, ValueError):
            pass
    return n
