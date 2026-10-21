"""Probe how job boards mark easy/quick apply vs the employer's site (not part of the test suite)."""
import csv, json, re, sys, tempfile, time
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import sitebrowse, util  # noqa: E402

DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
rows = list(csv.DictReader(open(DATA / "tracker.csv", encoding="utf-8-sig")))
li = [r["url"] for r in rows if r.get("source") == "LinkedIn"][:8]
print("== LinkedIn job pages")
for u in li:
    m = re.search(r"-(\d{6,})/?$", u.split("?")[0]) or re.search(r"/jobs/view/(\d{6,})", u)
    st, body = util.http(f"https://www.linkedin.com/jobs/view/{m.group(1)}")
    marks = {k: (k in body) for k in ("applyUrl", "apply-link-offsite", "apply-link-simple", "offsite", "Easy Apply", "easy-apply",
                                       "public_jobs_apply-link-onsite", "sign-up-modal__company_webiste", "apply-button")}
    ctl = sorted(set(re.findall(r'data-tracking-control-name="(public_jobs_apply[^"]*)"', body)))
    print(st, u[-40:], {k: v for k, v in marks.items() if v}, ctl)
    time.sleep(1.5)

PROBE = r"""(() => {
  const d = window.mosaic && window.mosaic.providerData && window.mosaic.providerData["mosaic-provider-jobcards"];
  const rs = (((d || {}).metaData || {}).mosaicProviderJobCardsModel || {}).results || [];
  return rs.slice(0, 6).map(r => ({t: r.displayTitle, keys: Object.keys(r).filter(k => /apply|employer|third|indeed|easy|ats/i.test(k)).map(k => k + "=" + JSON.stringify(r[k]).slice(0, 60))}));
})()"""
BAYT = r"""[...document.querySelectorAll('li[data-js-job]')].slice(0, 12).map(c => ({t: (c.querySelector('h2')||{}).textContent?.trim(), easy: /easy apply/i.test(c.innerText), btn: [...c.querySelectorAll('a,button,span')].map(x=>x.textContent.trim()).filter(x=>/apply/i.test(x)).slice(0,3)}))"""
NAUK = r"""[...document.querySelectorAll('.srp-tuple')].slice(0, 8).map(c => ({t: (c.querySelector('.designation-title')||{}).textContent?.trim(), apply: [...c.querySelectorAll('a,button,span,div')].map(x=>x.textContent.trim()).filter(x=>/apply/i.test(x)&&x.length<40).slice(0,3), txt: c.innerText.replace(/\s+/g,' ').slice(0,160)}))"""
sitebrowse.PROFILE_DIR = Path(tempfile.mkdtemp(prefix="jobagent-probe-")) / "browser-search"
sess = sitebrowse._session({"show_window": False})
try:
    sess.start(); tab = sess.tab()
    for name, url, js in (("indeed", "https://sa.indeed.com/jobs?q=civil+engineer&sort=date", PROBE),
                          ("bayt", "https://www.bayt.com/en/saudi-arabia/jobs/civil-engineer-jobs/", BAYT),
                          ("naukri", "https://www.naukrigulf.com/civil-engineer-jobs-in-saudi-arabia", NAUK)):
        sitebrowse._goto(tab, url, settle=2)
        time.sleep(3)
        print("==", name); print(json.dumps(sitebrowse._eval(tab, js), ensure_ascii=False, indent=0)[:3000])
finally:
    sitebrowse.close()
