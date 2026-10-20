"""Live check of the browser-read job boards on THIS computer (Edge/Chrome, your own network) - not part of the test suite.

    python tests/live_boards.py [board ...]      (default: all boards)   -> prints what each board returned

Each board is read once for one country with two search phrases, in the hidden search browser, exactly as a search does.
Nothing is written to a data folder except a temporary browser profile under the system temp directory.
"""
import json, sys, tempfile, time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # Arabic company names on a Windows console

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import sitebrowse, util  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
profile = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
boards = sys.argv[1:] or list(sitebrowse.SITES)
sitebrowse.PROFILE_DIR = Path(tempfile.mkdtemp(prefix="jobagent-live-")) / "browser-search"
util.KNOWN = set()
sess = sitebrowse._session({"show_window": False})
out = {}
try:
    sess.start()
    for key in boards:
        if key not in sitebrowse.SITES:
            print(f"unknown board {key}"); continue
        site = sitebrowse.SITES[key]
        # one country per board, two phrases, one page
        one = next((c for c in ("Saudi Arabia", "United Arab Emirates", "Canada", "Australia") if site["where"](c)), None)
        p = json.loads(json.dumps(profile))
        for c, v in p["search"]["countries"].items():
            if isinstance(v, dict):
                v["search"] = (c == one)
        t0 = time.time()
        jobs = sitebrowse._read_site(key, {"queries": ["civil engineer", "site engineer"], "pages": 1, "group_pages": 2}, p, sess)
        took = round(time.time() - t0, 1)
        sample = [{k: j.get(k, "") for k in ("title", "company", "location", "country", "posted", "url")} for j in jobs[:4]]
        detail = ""
        if jobs and site["detail"]:
            try:
                j = dict(jobs[0]); sitebrowse.enrich(j)
                detail = f"{len(j.get('description', ''))} chars, posted {j.get('posted') or '-'}"
            except Exception as e:
                detail = f"failed: {type(e).__name__}: {e}"
        out[key] = {"jobs": len(jobs), "seconds": took, "blocked": site["name"] in sitebrowse.BLOCKED, "sample": sample,
                    "posted_order": [j.get("posted", "") for j in jobs[:12]], "full_ad": detail}
        print(f"{site['name']:12s} {len(jobs):3d} jobs in {took}s  blocked={site['name'] in sitebrowse.BLOCKED}  full ad: {detail or '-'}")
        for s in sample:
            print(f"   - {s['title'][:50]!r} | {s['company'][:30]!r} | {s['location'][:30]!r} | {s['country']} | {s['posted']}")
finally:
    sitebrowse.close()
(Path(tempfile.gettempdir()) / "jobagent-live-boards.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
print("written:", Path(tempfile.gettempdir()) / "jobagent-live-boards.json")
