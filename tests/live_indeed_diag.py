"""Diagnose what Indeed shows this computer for a few search URLs (not part of the test suite)."""
import json, sys, tempfile, time
from pathlib import Path
from urllib.parse import quote_plus

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import sitebrowse  # noqa: E402

JS = r"""(() => {
  const d = window.mosaic && window.mosaic.providerData && window.mosaic.providerData["mosaic-provider-jobcards"];
  const m = ((d || {}).metaData || {}).mosaicProviderJobCardsModel || {};
  const rs = m.results || [];
  const count = (document.querySelector('.jobsearch-JobCountAndSortPane-jobCount, [data-testid="searchCount"], .jobsearch-JobCountAndSortPane') || {}).textContent || "";
  return { title: document.title, modelResults: rs.length, expired: rs.filter(r => r.expired).length,
           cards: document.querySelectorAll('.job_seen_beacon').length, dataJk: document.querySelectorAll('[data-jk]').length,
           countText: count.replace(/\s+/g, ' ').trim().slice(0, 80), totalInModel: m.totalResults || m.tier || null,
           firstTitles: rs.slice(0, 5).map(r => r.displayTitle || r.title), hasMosaic: !!window.mosaic,
           bodyStart: (document.body.innerText || '').slice(0, 200).replace(/\s+/g, ' ') };
})()"""

sitebrowse.PROFILE_DIR = Path(tempfile.mkdtemp(prefix="jobagent-diag-")) / "browser-search"
sess = sitebrowse._session({"show_window": False})
urls = {
    "or-21d": "https://sa.indeed.com/jobs?q=" + quote_plus('"civil engineer" OR "site engineer"') + "&sort=date&fromage=21",
    "plain-21d": "https://sa.indeed.com/jobs?q=" + quote_plus("civil engineer") + "&sort=date&fromage=21",
    "plain-any": "https://sa.indeed.com/jobs?q=" + quote_plus("civil engineer") + "&sort=date",
    "or-noquote": "https://sa.indeed.com/jobs?q=" + quote_plus("civil engineer OR site engineer") + "&sort=date&fromage=21",
    "ae-plain": "https://ae.indeed.com/jobs?q=" + quote_plus("civil engineer") + "&sort=date&fromage=21",
}
try:
    sess.start()
    tab = sess.tab()
    for name, url in urls.items():
        t0 = time.time()
        sitebrowse._goto(tab, url, settle=0.5)
        rows = sitebrowse._list_rows(tab, sitebrowse.INDEED_JS, "Indeed") if True else []
        info = sitebrowse._eval(tab, JS)
        print(f"== {name}: extractor rows={len(rows)} in {time.time() - t0:.1f}s")
        print("   ", json.dumps(info, ensure_ascii=False)[:900])
        time.sleep(2)
finally:
    sitebrowse.close()
