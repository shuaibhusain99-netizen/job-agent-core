#!/usr/bin/env python3
"""Bridge between the repo (tracker.csv, applications/, profile.json) and the control panel's store.

Subcommands (all read/write only local files; the caller moves data to/from the store):

  export-writes --db-dir DIR --out-dir OUT
      DIR holds the store's current docs as JSON files (jobs/<id>.json, each with "version").
      1. merges status / notes / applied_date edited in the panel back into tracker.csv
      2. writes OUT/writes_N.json batches (<=50 each) creating store docs for jobs not yet there
         and refreshing packs for existing docs whose pack changed
  apply-settings SETTINGS_JSON
      applies the panel's config/settings doc to profile.json; prints {"changed":bool,"run_now":bool,"paused":bool}
  meta --out FILE
      builds the meta/sync document body from tracker.csv and git log
"""
from __future__ import annotations
import argparse, csv, hashlib, json, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACKER = ROOT / "tracker.csv"
PROFILE = ROOT / "profile.json"
PACKS = ROOT / "applications"
FIELDS = ["found_date", "score", "verdict", "title", "company", "location", "country", "source",
          "url", "status", "applied_date", "notes", "pack_file"]


def job_id(url: str) -> str:
    return hashlib.sha1(url.strip().lower().encode()).hexdigest()[:16]


def read_tracker() -> list[dict]:
    if not TRACKER.exists():
        return []
    with TRACKER.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("url")]


def write_tracker(rows: list[dict]) -> None:
    with TRACKER.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def parse_pack(path: Path) -> dict:
    if not path.exists():
        return {}
    t = path.read_text(encoding="utf-8")
    def section(name):
        m = re.search(rf"## {re.escape(name)}\n+([\s\S]*?)(?=\n## |\Z)", t)
        return m.group(1).strip() if m else ""
    answers = []
    for q, a in re.findall(r"\*\*Q:\*\* (.+?)\n\*\*A:\*\* (.+?)(?:\n\n|\Z)", section("Draft screening answers"), re.S):
        answers.append({"q": q.strip(), "a": a.strip()})
    return {"summary": section("CV summary for this application"), "cover_letter": section("Cover letter"), "answers": answers}


def cmd_export(args):
    db_dir = Path(args.db_dir)
    existing = {}
    for p in (db_dir / "jobs").glob("*.json") if (db_dir / "jobs").exists() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            existing[p.stem] = d
        except json.JSONDecodeError:
            pass
    rows = read_tracker()
    merged_back = 0
    writes = []
    for r in rows:
        jid = job_id(r["url"])
        pack = parse_pack(ROOT / r["pack_file"]) if r.get("pack_file") else {}
        doc = existing.get(jid)
        if doc:
            body = doc.get("data", doc)  # tolerate either {"data":..,"version":..} or flat
            # 1. panel edits win for status / applied_date / user notes
            for k in ("status", "applied_date"):
                if body.get(k) and body[k] != r.get(k):
                    r[k] = body[k]
                    merged_back += 1
            # 2. refresh pack if the repo has one and the store doesn't
            if pack and not body.get("pack"):
                writes.append({"op": "update", "collection": "jobs", "doc_id": jid,
                               "if_version": doc.get("version"), "data": {"pack": pack}})
            continue
        writes.append({"op": "set", "collection": "jobs", "doc_id": jid, "data": {
            "found_date": r["found_date"], "score": int(float(r["score"] or 0)), "verdict": r["verdict"],
            "title": r["title"], "company": r["company"], "location": r["location"], "country": r["country"],
            "source": r["source"], "url": r["url"], "status": r["status"] or "New", "applied_date": r.get("applied_date", ""),
            "reasons": r.get("notes", ""), "notes_user": "", "pack": pack,
        }})
    write_tracker(rows)
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("writes_*.json"):
        f.unlink()
    for i in range(0, len(writes), 50):
        (out / f"writes_{i // 50 + 1}.json").write_text(json.dumps({"writes": writes[i:i + 50]}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"tracker_rows": len(rows), "store_docs": len(existing), "new_writes": len(writes),
                      "batches": (len(writes) + 49) // 50, "merged_back": merged_back}))


def cmd_apply_settings(args):
    s = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    s = s.get("data", s)
    p = json.loads(PROFILE.read_text(encoding="utf-8"))
    before = json.dumps(p, sort_keys=True)
    if isinstance(s.get("countries"), dict):
        for name, cfg in s["countries"].items():
            if name in p["search"]["countries"] and isinstance(cfg, dict):
                p["search"]["countries"][name] = {"weight": float(cfg.get("weight", 1)), "search": bool(cfg.get("search", True)), "apply": bool(cfg.get("apply", True))}
    if isinstance(s.get("min_score"), (int, float)):
        p["search"]["min_score"] = int(s["min_score"])
    p["search"]["paused"] = bool(s.get("paused", False))
    changed = json.dumps(p, sort_keys=True) != before
    if changed:
        PROFILE.write_text(json.dumps(p, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"changed": changed, "run_now": bool(s.get("run_now", False)), "paused": bool(s.get("paused", False))}))


def cmd_meta(args):
    rows = read_tracker()
    try:
        last = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%cI%n%s", "--", "tracker.csv"], capture_output=True, text=True).stdout.strip().split("\n")
        last_run, msg = (last + ["", ""])[:2]
    except Exception:
        last_run, msg = "", ""
    today = datetime.now(timezone.utc).date().isoformat()
    new_today = sum(1 for r in rows if r["found_date"] == today)
    body = {"last_sync": datetime.now(timezone.utc).isoformat(timespec="minutes"), "last_run": last_run[:10],
            "message": f"Tracker: {len(rows)} jobs, {new_today} found today. Last agent commit: {msg}"}
    Path(args.out).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(body))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("export-writes"); a.add_argument("--db-dir", required=True); a.add_argument("--out-dir", required=True); a.set_defaults(fn=cmd_export)
    b = sub.add_parser("apply-settings"); b.add_argument("settings"); b.set_defaults(fn=cmd_apply_settings)
    c = sub.add_parser("meta"); c.add_argument("--out", required=True); c.set_defaults(fn=cmd_meta)
    args = ap.parse_args(); args.fn(args)


if __name__ == "__main__":
    main()
