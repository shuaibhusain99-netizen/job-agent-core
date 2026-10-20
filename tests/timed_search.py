"""One search exactly as the app runs it (data folder = ./data next to app.py), each log line time-stamped. Not part of the test suite."""
import io, json, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import app

app.ensure_data()
import main as m
import agent.sources as srcmod

m.ROOT = app.DATA
m.PROFILE = json.loads((app.DATA / "profile.json").read_text(encoding="utf-8"))
m.SOURCES = json.loads((app.DATA / "sources.json").read_text(encoding="utf-8"))
m.TRACKER, m.REPORTS, m.PACKS = app.DATA / "tracker.csv", app.DATA / "reports", app.DATA / "applications"
srcmod.KEYS_PATH = app.DATA / "keys.json"
real, t0 = sys.__stdout__, time.time()


class Stamp(io.TextIOBase):
    def write(self, s):
        for line in s.splitlines():
            if line.strip():
                real.write(f"{time.time() - t0:6.1f} {line}\n"); real.flush()
        return len(s)


sys.stdout = sys.stderr = Stamp()
sys.argv = ["main.py"] + sys.argv[1:]
try:
    m.main()
finally:
    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    print(f"TOTAL {time.time() - t0:.0f} s")
