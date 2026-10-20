"""Opens one job in the apply window exactly as 'Apply on site' does (own temp profile) and reports which tab shows it.
Not part of the test suite. Usage: python tests/live_apply_window.py <job url>"""
import json, sys, tempfile, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import browser  # noqa: E402
prof = json.loads((Path(__file__).resolve().parents[1] / "profile.json").read_text(encoding="utf-8"))
url = sys.argv[1]
s = browser.ApplySession(url, prof, {"title": "x", "company": "y", "country": "Canada", "location": ""}, None, 4,
                         profile_dir=Path(tempfile.mkdtemp(prefix="jobagent-apply-")))
time.sleep(8)
tabs = [t for t in json.loads(urllib.request.urlopen(f"http://127.0.0.1:{s.port if hasattr(s, 'port') else browser._STATE['port']}/json/list").read()) if t["type"] == "page"]
print("page tabs:", [(t["url"][:70], t["title"][:40]) for t in tabs])
print("helper state:", s._eval("window.__ja ? __ja.state() : null"), "| log:", s.log[-3:])
time.sleep(4)
s.stop = True
browser.close_browser()
