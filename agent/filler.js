/* JobAgent form filler - runs in an ISOLATED WORLD of the job page (set up by agent/browser.py).
   The page shares the DOM but cannot see these variables or patch these built-ins.
   Rules: auto-fill only on known job boards / ATS sites (elsewhere only after you click "Fill this page", and then only
   fields you can actually see, not covered by anything); the CV only after you click "Attach CV"; never ticks consent
   or legal declarations; never clicks Submit/Next/any button - only radio buttons and checkboxes of a Yes/No question;
   a job counts as Applied only when YOU press "I submitted". The app reads __ja.state() to know what happened. */
(() => {
  if (window.__ja) return;
  const CTX = __JOBAGENT_CONTEXT__;
  const HOST = (location.hostname || "").toLowerCase();
  const AUTO = (CTX.hosts || []).some(h => HOST === h || HOST.endsWith("." + h));
  if (window !== window.top) return;            // forms inside other sites' frames are left alone
  const P = CTX.profile, C = P.candidate, F = P.form_answers || {}, JOB = CTX.job || {};
  const GCC = ["Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Oman", "Bahrain"];
  const inGCC = JOB.country ? GCC.includes(JOB.country) : null;     // unknown country: work-permit questions stay yours
  const WORK = (gcc, other) => inGCC === null ? null : (inGCC ? gcc : other);
  const nameParts = (C.name || "").trim().split(/\s+/);
  const first = F.first_name || nameParts[0] || "", last = F.last_name || nameParts.slice(1).join(" ") || "";
  const [city, country] = (C.location || "Sitra, Bahrain").split(",").map(s => s.trim());
  const phoneDigits = (C.phone || "").replace(/[^\d+]/g, "");
  const localPhone = phoneDigits.replace(/^\+?973/, "");
  const coverLetter = CTX.cover_letter || "";
  const answersRx = Object.entries(P.screening_answers || {}).filter(([k]) => !k.startsWith("_")).map(([k, v]) => [new RegExp(k, "i"), v]);
  const nationality = F.nationality || "Bahraini";
  const YEARS = String(CTX.years ?? C.years_experience ?? "");

  // ---- text-value rules: [regex on the field's descriptor, value]. First match wins.
  const RULES = [
    [/\b(first|given)\s*name\b/i, first],
    [/\b(last|family|sur)\s*name\b/i, last],
    [/\bmiddle\s*name\b/i, F.middle_name || (nameParts.length > 2 ? nameParts[1] : "")],
    [/\b(full\s*name|your name|^name$|candidate name|applicant name)\b/i, C.name],
    [/\be-?mail\b/i, C.email],
    [/\b(phone|mobile|contact number|telephone|cell)\b/i, (d) => /without|excluding|local|national/i.test(d) ? localPhone : C.phone],
    [/\b(country|dial(ing)?|phone)\s*code\b|\bprefix\b/i, "+973"],
    [/\bcity\b|\btown\b/i, city],
    [/\b(state|province|region|emirate)\b/i, F.region || "Capital Governorate"],
    [/\b(postal|zip|post)\s*code\b/i, F.postal_code || ""],
    [/\bnationalit(y|ies)\b|\bcitizenship\b/i, nationality],
    [/\bcountry\b.*\b(residence|location|living|current)\b|\b(residence|location)\b.*\bcountry\b|^country$|\bcountry\b/i, country],
    [/\baddress\b/i, F.address || `${city}, ${country}`],
    [/\blinked\s*in\b/i, F.linkedin || ""],
    [/\b(website|portfolio|github|url)\b/i, ""],
    [/\b(current|present|latest|most recent|last)\b.*\b(job\s*title|position|role|designation)\b|\b(job\s*title|designation)\b/i, F.current_title || "Project Engineer"],
    [/\b(current|present|latest|most recent|last)\b.*\b(company|employer|organi[sz]ation)\b|\b(company|employer)\s*name\b/i, F.current_company || "Al Door Excavation & Building Contracting"],
    [/\b(total|years?|yrs?)\b.*\bexperience\b|\bexperience\b.*\b(years?|yrs?)\b/i, (d, el) => el.type === "number" || /^\s*$/.test(el.placeholder || "") && el.tagName !== "TEXTAREA" ? YEARS : `${YEARS} years`],
    [/\bnotice\s*period\b|\bavailab(le|ility)\b.*\b(start|join|date)\b|\bstart\s*date\b|\bjoining\b/i, F.notice_text || `${C.notice_period}`],
    [/\b(expected|desired|target)\b.*\b(salary|pay|compensation|ctc)\b|\bsalary\s*expectation/i, F.expected_salary || "Negotiable"],
    [/\bcurrent\b.*\b(salary|pay|ctc)\b/i, F.current_salary || ""],
    [/\b(university|college|school|institution|institute)\b/i, F.university || "University of Bahrain"],
    [/\b(degree|qualification|education\s*level|highest)\b/i, F.degree_short || "Bachelor's degree"],
    [/\b(major|field of study|specialis|discipline|course)\b/i, "Civil Engineering"],
    [/\b(graduat|completion|end)\w*\s*(year|date)\b|\byear\s*of\s*(graduation|passing)\b/i, F.grad_year || "2021"],
    [/\bgpa\b|\bgrade\b|\bcgpa\b/i, F.gpa || "3.17"],
    [/\b(cover\s*letter|covering letter|motivation|why (do you|are you|should we)|tell us|about yourself|introduce yourself|summary|message to|additional information|comments?)\b/i, (d) => answerFor(d) || coverLetter],
    [/\b(skills?|certifications?|software)\b/i, (d) => answerFor(d) || (C.software || []).join(", ")],
    [/\b(languages?)\b/i, (C.languages || []).join(", ")],
    [/\b(headline|profile title|title)\b/i, C.headline],
    [/\bname\b/i, C.name],
  ];
  // ---- yes/no questions (radios, selects, checkboxes with Yes/No options)
  const YESNO = [
    [/\b(authori[sz]ed|legally|eligible|permitted|right)\b.*\bwork\b|\bwork\s*(permit|authori[sz]ation)\b/i, WORK("Yes", "No")],
    [/\bsponsor/i, WORK("No", "Yes")],
    [/\bvisa\b/i, WORK("No", "Yes")],
    [/\brelocat|\bwilling to (move|travel|work)|\bonsite|\bon-site|\bshift|\brotation|\bovertime/i, "Yes"],
    [/\b(18|eighteen)\b|\blegal age\b|\badult\b/i, "Yes"],
    // a licence of a named foreign country / an international permit is yours to answer
    [/\bdriv(ing|er'?s?)\s*licen[cs]e\b/i, (d) => /saudi|uae|emirat|qatar|kuwait|oman|canad|austral|international|\bidp\b|heavy|hgv|forklift/i.test(d) ? null : "Yes"],
    [/\bnebosh|iosh|safety\s*(cert|card)|\bhse\b/i, "Yes"],
    // a bachelor's he has; a master's, PhD, MBA, chartership or a named other discipline he has not
    [/\b(master|msc|m\.sc|phd|doctor|mba|post-?grad|chartered|licensed)\b/i, "No"],
    [/\b(bachelor|degree|engineering degree|b\.?sc)\b/i, (d) => /\b(electrical|mechanical|chemical|petroleum|computer|software|architect)/i.test(d) ? "No" : "Yes"],
    [/\bautocad|civil ?3d|ms project|excel\b/i, "Yes"],
    [/\bcivil engineer(ing)?\b.*\b(experience|background)\b/i, "Yes"],
    [/\bsaudi council|\bsce\b/i, F.sce_registered || "No"],
    [/\bp\.?\s*eng\b|\bprofessional engineer\b|\bchartered\b|\brpeq\b|\bcpeng\b/i, "No"],
    [/\bbased in\b|\bcurrently (reside|living|located)\b|\blocal candidate\b/i, inGCC === null ? null : (inGCC && /bahrain/i.test(JOB.location || "") ? "Yes" : "No")],
  ];
  // Questions only YOU can answer (legal declarations, consent, history, diversity) are never filled.
  const PERSONAL = /\bconvict|\bcriminal|\bfelony|\boffen[cs]e|\bdisabilit|\bveteran|\bgender|\bsex\b|\bethnic|\brace\b|\breligio|\bmarital|\bpregnan|\bdate of birth|\bdob\b|\bage\b|\brelative|\bfamily member|\bconflict of interest|\bgovernment official|\bpreviously (worked|applied|employed)|\bformer employee|\bnon-?compete|\bemployment agreement|\brestrictive|\bagree\b|\bconsent|\backnowledge|\bcertify|\bdeclar|\bprivacy|\bterms\b|\bsignature|\bsign here|\bpassword|\bpassport|\bnational id|\bcpr\b|\biqama|\bssn\b|\bsocial security|\bbank|\biban|\bcard number/i;

  function answerFor(d) {
    for (const [rx, v] of answersRx) if (rx.test(d)) return v;
    return "";
  }
  // A field counts only if a person could see it: real size, inside the page horizontally, not transparent,
  // not clipped away, not aria-hidden. Off-screen "honeypot" inputs are never filled.
  const boxOf = (el) => (el.tagName === "INPUT" && (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list")
    && el.parentElement && (el.parentElement.closest('[class*="control"],[class*="Control"]') ||
        el.parentElement.closest('[class*="select__container"],[class*="select-container"],[data-automation-id*="select"],[role=group]'))) || el;
  function vis(el0) {
    const el = boxOf(el0);
    const r = el.getBoundingClientRect();
    const small = el.type === "radio" || el.type === "checkbox";
    if (r.width < (small ? 10 : 24) || r.height < (small ? 10 : 12)) return false;
    const docW = Math.max(document.documentElement.scrollWidth, innerWidth);
    if (r.right + scrollX <= 0 || r.left + scrollX >= docW || r.bottom + scrollY <= 0) return false;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.display === "none" || s.visibility === "hidden" || +s.opacity < 0.15) return false;
      if (n.getAttribute("aria-hidden") === "true" || n.hasAttribute("inert")) return false;
      if (/inset\(\s*(50|100)%/.test(s.clipPath) || /rect\(\s*0(px)?,?\s*0(px)?,?\s*0(px)?,?\s*0(px)?\s*\)/.test(s.clip)) return false;
    }
    return AUTO || uncovered(el, r);
  }
  // Outside known job sites a field must be in view and not covered by another element (a page could hide a field
  // under a picture to collect your details without you seeing them). Fields further down fill as you scroll to them.
  function uncovered(el, r) {
    const x = r.left + Math.min(r.width / 2, 20), y = r.top + Math.min(r.height / 2, 10);
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return false;
    const hit = document.elementFromPoint(x, y);
    if (!hit) return false;
    if (hit === el || el.contains(hit) || hit.contains(el)) return true;
    const lab = hit.closest("label");
    return !!(lab && (lab.contains(el) || (el.id && lab.htmlFor === el.id)));
  }
  const txt = (n) => (n ? (n.innerText || n.textContent || "") : "").replace(/\s+/g, " ").trim();
  function describe(el) {
    const parts = [];
    let labelled = false;
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) { parts.push(txt(l)); labelled = true; } }
    const pl = el.closest("label"); if (pl) { parts.push(txt(pl)); labelled = true; }
    if (el.getAttribute("aria-labelledby")) { parts.push(el.getAttribute("aria-labelledby").split(/\s+/).map(id => txt(document.getElementById(id))).join(" ")); labelled = true; }
    for (const a of ["aria-label", "placeholder", "title"]) { const v = el.getAttribute(a); if (v) { parts.push(v); labelled = true; } }
    for (const a of ["name", "id", "data-automation-id", "data-test", "data-testid"]) { const v = el.getAttribute(a); if (v && !/^[a-z0-9_-]{0,3}$|^\d|^(input|field|text|select|q|question)[-_]?\d*$/i.test(v)) parts.push(v.replace(/[-_]+/g, " ")); }
    if (el.getAttribute("aria-describedby")) parts.push(el.getAttribute("aria-describedby").split(/\s+/).map(id => txt(document.getElementById(id))).join(" "));
    if (!labelled) {
      // no explicit label: use the nearest short text block above the field (question text, legend, heading)
      let n = el, hops = 0, found = false;
      while (n && hops < 5 && !found) {
        for (let p = n.previousElementSibling; p; p = p.previousElementSibling) {
          if (p.querySelector("input,select,textarea,button")) break;
          const t = txt(p); if (t && t.length < 220) { parts.push(t); found = true; break; }
        }
        if (found) break;
        const par = n.parentElement; if (!par) break;
        if (par.tagName === "FIELDSET" || par.getAttribute("role") === "group" || par.getAttribute("role") === "radiogroup") {
          const lg = par.querySelector("legend") || (par.getAttribute("aria-labelledby") ? document.getElementById(par.getAttribute("aria-labelledby")) : null);
          if (lg) { parts.push(txt(lg)); found = true; break; }
          if (par.getAttribute("aria-label")) { parts.push(par.getAttribute("aria-label")); found = true; break; }
        }
        n = par; hops++;
      }
    }
    return parts.filter(Boolean).join(" | ").slice(0, 400);
  }
  const SET = new WeakMap();     // what we put in each field, so a framework re-render that wipes it can be redone
  const DONE = new WeakSet();    // fields already handled (kept here, not in the page's DOM)
  function setValue(el, value) {
    SET.set(el, { v: value, n: (SET.get(el) || { n: 0 }).n + 1 });
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value");
    el.focus();
    if (setter && setter.set) setter.set.call(el, value); else el.value = value;
    for (const t of ["input", "change", "keyup", "blur"]) el.dispatchEvent(new Event(t, { bubbles: true }));
    DONE.add(el);
  }
  function chooseOption(sel, wanted) {
    const w = String(wanted).toLowerCase();
    const opts = [...sel.options];
    let o = opts.find(o => o.text.trim().toLowerCase() === w) || opts.find(o => o.text.trim().toLowerCase().startsWith(w)) || opts.find(o => o.text.toLowerCase().includes(w));
    if (!o && /^\+?\d+$/.test(w)) o = opts.find(o => o.text.replace(/\s/g, "").includes(w.replace("+", "")) || o.value.includes(w.replace("+", "")));
    if (!o) return false;
    sel.focus(); sel.value = o.value;
    for (const t of ["input", "change", "blur"]) sel.dispatchEvent(new Event(t, { bubbles: true }));
    DONE.add(sel);
    return true;
  }
  const isCombo = (el) => el.tagName === "INPUT" && !el.closest('[class*="iti"]') && (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" || /select__input|react-select|combobox|autocomplete/i.test((el.className || "") + " " + (el.parentElement && el.parentElement.className || "")));
  const COMBOS = [];               // dropdowns the app drives with real mouse/keyboard input
  let CUR = null, OPT = null;
  function fillCombo(el, want) { DONE.add(el); COMBOS.push({ el, want }); }
  function valueFor(el, d) {
    for (const [rx, v] of RULES) if (rx.test(d)) return typeof v === "function" ? v(d, el) : v;
    return null;
  }
  function yesNoFor(d) { for (const [rx, v] of YESNO) if (rx.test(d)) return typeof v === "function" ? v(d) : v; return null; }   // null = leave it to you

  // the only element the filler ever clicks: one option of a Yes/No (or similar) question - never a button or link
  function tick(g) {
    if (!(g instanceof HTMLInputElement) || (g.type !== "radio" && g.type !== "checkbox") || g.disabled) return false;
    g.click();
    return true;
  }
  function fillOnce() {
    let filled = 0;
    const seenGroups = new Set();
    for (const el of document.querySelectorAll("input, textarea, select")) {
      if (!vis(el) || el.disabled || el.readOnly) continue;
      if (DONE.has(el)) {
        const prev = SET.get(el);
        if (prev && prev.n < 4 && el.tagName !== "SELECT" && !el.value) { setValue(el, prev.v); filled++; }   // re-render wiped it
        continue;
      }
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["hidden", "submit", "button", "file", "password", "search", "image", "reset"].includes(type)) continue;
      let d = describe(el);
      if (type === "radio" || (type === "checkbox" && el.name && document.querySelectorAll(`input[type="checkbox"][name="${CSS.escape(el.name)}"]`).length > 1)) {
        const fs = el.closest("fieldset,[role=group],[role=radiogroup]");
        const lg = fs ? (fs.querySelector("legend") || (fs.getAttribute("aria-labelledby") ? document.getElementById(fs.getAttribute("aria-labelledby")) : null)) : null;
        if (lg) d = txt(lg) + " | " + d; else if (fs && fs.getAttribute("aria-label")) d = fs.getAttribute("aria-label") + " | " + d;
        else { let p = (el.closest("label") || el).parentElement, q = ""; for (let i = 0; p && i < 4 && !q; i++, p = p.parentElement) { for (let s = p.previousElementSibling; s; s = s.previousElementSibling) { if (s.querySelector("input,select,textarea")) break; const t = txt(s); if (t && t.length < 220) { q = t; break; } } } if (q) d = q + " | " + d; }
      }
      if (!d || PERSONAL.test(d)) { DONE.add(el); continue; }
      if (type === "radio" || type === "checkbox") {
        const key = el.name || d; if (seenGroups.has(key)) continue;
        const group = (el.name ? [...document.querySelectorAll(`input[type="${type}"][name="${CSS.escape(el.name)}"]`)] : [el])
          .filter(g => g.type === "radio" || g.type === "checkbox");
        if (group.some(g => g.checked)) { seenGroups.add(key); continue; }
        const labels = group.map(g => ({ g, t: (txt(g.closest("label")) || txt(document.querySelector(`label[for="${CSS.escape(g.id || "")}"]`)) || "").toLowerCase() }));
        const q = d + " " + labels.map(l => l.t).join(" ");
        let want = yesNoFor(q);
        if (type === "checkbox" && group.length === 1) { seenGroups.add(key); DONE.add(el); continue; }   // single tick boxes are yours to tick
        if (PERSONAL.test(q)) { seenGroups.add(key); group.forEach(g => DONE.add(g)); continue; }
        if (want) {
          const wl = want.toLowerCase();
          let pick = labels.find(l => l.t === wl) || labels.find(l => l.t.startsWith(wl)) || labels.find(l => l.t.includes(wl));
          if (!pick) { // non yes/no groups: country / degree etc.
            const v = valueFor(el, d); if (v) pick = labels.find(l => l.t.includes(String(v).toLowerCase()));
          }
          if (pick && tick(pick.g)) { group.forEach(g => DONE.add(g)); filled++; }
        } else {
          const v = valueFor(el, d); const pick = v ? labels.find(l => l.t.includes(String(v).toLowerCase())) : null;
          if (pick && tick(pick.g)) { group.forEach(g => DONE.add(g)); filled++; }
        }
        seenGroups.add(key); continue;
      }
      if (el.tagName === "SELECT") {
        if (el.value && el.selectedIndex > 0 && !/select|choose|please/i.test(el.options[el.selectedIndex].text)) continue;
        const yn = yesNoFor(d); const v = yn && [...el.options].some(o => /^(yes|no)$/i.test(o.text.trim())) ? yn : valueFor(el, d);
        if (v && chooseOption(el, v)) filled++;
        continue;
      }
      if (el.value && el.value.trim()) continue;        // never overwrite what is already there
      if (isCombo(el)) {                                 // react-select style dropdown: type, then pick the matching option
        if (!AUTO) continue;                             // only on known job sites: elsewhere a dropdown is yours to pick
        const yn = yesNoFor(d); let want = yn || valueFor(el, d);
        if (want) { fillCombo(el, String(want)); filled++; }
        continue;
      }
      let v = valueFor(el, d);
      if (v == null && el.tagName === "TEXTAREA") v = answerFor(d) || null;
      if (v == null && /\?/.test(d)) { const yn = yesNoFor(d); v = yn ? (answerFor(d) || yn) : answerFor(d) || null; }
      if (v) { setValue(el, String(v)); filled++; }
    }
    return filled;
  }
  const SUBMITTED = /application (was )?(sent|submitted|received)|thank you for (applying|your application)|successfully (applied|submitted)|you('ve| have) applied|application complete/i;
  const ST = { total: 0, filledNow: 0, submitted: false, cvRequested: false, cvCount: 0, armed: AUTO };
  let bar, msg, barSeen = false;
  const BAR_CSS = "all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483647";
  const BAR_NORM = (() => { const d = document.createElement("div"); d.style.cssText = BAR_CSS; return d.style.cssText; })();   // as Chrome writes it back
  // The bar lives in a closed shadow root: the page can't read it or find its buttons. The page CAN restyle or cover the
  // host element, so a click counts only while Chrome reports the bar fully visible and unobscured (IntersectionObserver
  // v2 trackVisibility - the browser's own clickjacking check) and the click really landed on it.
  function bar_() {
    if (bar && document.documentElement.contains(bar)) return;
    bar = document.createElement("div");
    const root = bar.attachShadow({ mode: "closed" });
    bar.style.cssText = BAR_CSS;
    root.innerHTML = `<style>
      .b{background:#111827;color:#fff;font:13px/1.4 system-ui,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.35);display:flex;gap:8px;align-items:center;max-width:560px;flex-wrap:wrap}
      button{font:inherit;border:0;border-radius:6px;padding:5px 9px;color:#fff;cursor:pointer}
      .p{background:#2563eb}.g{background:#16a34a}.c{background:#374151}.x{background:none;color:#9ca3af}</style>
      <div class="b"><b>JobAgent</b><span id="m"></span>
      <button class="p" id="fill"></button><button class="c" id="cv">Attach CV</button><button class="g" id="done">I submitted ✓</button><button class="x" id="x">×</button></div>`;
    msg = root.getElementById("m");
    const fillBtn = root.getElementById("fill");
    fillBtn.textContent = ST.armed ? "Fill again" : "Fill this page";
    const trusted = (fn) => (e) => {                                        // only real clicks by the person, on a bar they can see
      const s = getComputedStyle(bar);
      const ok = e.isTrusted && barSeen && document.elementFromPoint(e.clientX, e.clientY) === bar && s.opacity === "1"
        && s.visibility === "visible" && s.display !== "none" && bar.style.cssText === BAR_NORM;
      if (ok) fn(); else { bar.style.cssText = BAR_CSS; barSeen = false; say("click ignored - the page was covering this bar; try again"); }
    };
    fillBtn.addEventListener("click", trusted(() => { ST.armed = true; fillBtn.textContent = "Fill again"; run(); }));
    root.getElementById("cv").addEventListener("click", trusted(() => { ST.cvRequested = true; say("attaching CV…"); }));
    root.getElementById("done").addEventListener("click", trusted(() => { ST.submitted = true; say("marked as Applied in the app ✓"); }));
    root.getElementById("x").addEventListener("click", trusted(() => bar.remove()));
    document.documentElement.appendChild(bar);
    try {
      const io = new IntersectionObserver(es => { for (const e of es) barSeen = e.isVisible === true; }, { trackVisibility: true, delay: 100 });
      io.observe(bar);
    } catch (e) { barSeen = false; }      // no visibility tracking: the bar can't be trusted, so its buttons do nothing
    say(ST.armed ? "ready" : `${HOST} isn't a known job site - check it, then press "Fill this page"`);
  }
  function say(m) { bar_(); msg.textContent = m; }
  function run() {
    if (!document.body) return;
    if (ST.armed) {
      const n = fillOnce(); ST.total += n; ST.filledNow += n;
      if (ST.total) say(`filled ${ST.total} field${ST.total > 1 ? "s" : ""} - check them, attach your CV, then press the site's button`);
      else say("no fields yet - open the apply form");
    }
    if (!ST.submitted && !ST.hinted && ST.total && SUBMITTED.test((document.body.innerText || "").slice(0, 20000))) {
      ST.hinted = true; say("looks sent - press \"I submitted ✓\" to mark it Applied in the app");
    }
  }
  function cvBoxes() {
    const all = [...document.querySelectorAll("input[type=file]")].filter(i => !i.disabled);
    const named = all.filter(i => /\b(cv|resume|résumé|curriculum)\b/i.test(describe(i) + " " + (i.accept || "")));
    const notOther = all.filter(i => !/\b(cover\s*letter|photo|picture|image|avatar|transcript|certificate|passport|id\b)/i.test(describe(i)));
    return named.length ? named : (notOther.length === 1 ? notOther : []);
  }
  window.__ja = {           // isolated-world API for the app (the page cannot see this object)
    state() { const s = { ...ST, combos: COMBOS.length }; ST.filledNow = 0; return s; },
    cvTarget(i) { return cvBoxes()[i] || null; },
    cvDone(n) { ST.cvRequested = false; ST.cvCount += n; say(n ? "CV attached ✓ - check it shows on the form" : "no CV upload box on this page"); },
    nextCombo() { CUR = COMBOS.shift() || null; return CUR ? CUR.want : null; },
    current() { return CUR && document.contains(CUR.el) && vis(CUR.el) ? CUR.el : null; },
    menuOpen() {
      const e = document.activeElement;
      return !!(e && (e.getAttribute("aria-expanded") === "true" || document.querySelector('[role=listbox] [role=option],[class*="select__option"],[class*="menu"] [class*="option"]')));
    },
    findOption(want) {
      const w = String(want).toLowerCase();
      const notPhone = o => !o.closest('[class*="iti"],[class*="flag"],[class*="dial"]');
      // an "option" must be a plain list entry - never a button, link, submit control or anything inside one
      const plain = o => !o.closest('button,a,input,select,textarea,label,[type="submit"],[role="button"],[role="link"],[role="menuitem"]');
      let opts = [...document.querySelectorAll('[role="option"]')].filter(o => vis(o) && notPhone(o) && plain(o));
      if (!opts.length) opts = [...document.querySelectorAll('[class*="option"],[class*="Option"]')].filter(o => vis(o) && notPhone(o) && plain(o) && !o.querySelector("input,button,a") && o.textContent.trim().length < 120);
      const t = opts.map(o => ({ o, t: o.textContent.replace(/\s+/g, " ").trim().toLowerCase() }));
      const yn = (w === "yes" || w === "no");
      const sw = t.filter(x => x.t === w || x.t.startsWith(w + " ") || x.t.startsWith(w + ",") || x.t.startsWith(w + " -") || x.t === w + ".").sort((a, b) => a.t.length - b.t.length);
      let h = t.find(x => x.t === w) || null;
      if (!h && sw.length === 1) h = sw[0]; else if (!h && sw.length > 1 && yn) { OPT = null; return "AMBIGUOUS"; }
      if (!h && !yn) h = t.find(x => x.t.startsWith(w)) || t.find(x => x.t.includes(w));
      OPT = h ? h.o : null;
      return h ? "OK" : (opts.length ? "NONE" : "NOMENU");
    },
    option() { return OPT; },
    arm() { ST.armed = true; run(); },          // the app's own "fill now" (the page cannot call this)
  };
  const mo = new MutationObserver(() => { clearTimeout(ST._t); ST._t = setTimeout(run, 700); });
  const start = () => {
    bar_(); setTimeout(run, 1200); setTimeout(run, 4000); mo.observe(document.documentElement, { childList: true, subtree: true });
    addEventListener("scroll", () => { clearTimeout(ST._s); ST._s = setTimeout(run, 400); }, { passive: true });   // fields you scroll to
  };
  if (document.readyState !== "complete") window.addEventListener("load", start); else start();
})();
