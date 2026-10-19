/* JobAgent form filler - injected into the job page by agent/browser.py.
   Fills fields it recognises (never clicks Submit/Next), shows a small bar, reports back to the app. */
(() => {
  if (window.__jobagent) return;
  const CTX = __JOBAGENT_CONTEXT__;
  const P = CTX.profile, C = P.candidate, F = P.form_answers || {}, JOB = CTX.job || {};
  const GCC = ["Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Oman", "Bahrain"];
  const inGCC = GCC.includes(JOB.country || "") || !JOB.country;
  const nameParts = (C.name || "").trim().split(/\s+/);
  const first = F.first_name || nameParts[0] || "", last = F.last_name || nameParts.slice(1).join(" ") || "";
  const [city, country] = (C.location || "Sitra, Bahrain").split(",").map(s => s.trim());
  const phoneDigits = (C.phone || "").replace(/[^\d+]/g, "");
  const localPhone = phoneDigits.replace(/^\+?973/, "");
  const coverLetter = CTX.cover_letter || "";
  const answersRx = Object.entries(P.screening_answers || {}).filter(([k]) => !k.startsWith("_")).map(([k, v]) => [new RegExp(k, "i"), v]);
  const nationality = F.nationality || "Bahraini";
  const todayYear = new Date().getFullYear();

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
    [/\b(total|years?|yrs?)\b.*\bexperience\b|\bexperience\b.*\b(years?|yrs?)\b/i, (d, el) => el.type === "number" || /^\s*$/.test(el.placeholder || "") && el.tagName !== "TEXTAREA" ? String(C.years_experience) : `${C.years_experience} years`],
    [/\bnotice\s*period\b|\bavailab(le|ility)\b.*\b(start|join|date)\b|\bstart\s*date\b|\bjoining\b/i, F.notice_text || `${C.notice_period}`],
    [/\b(expected|desired|target)\b.*\b(salary|pay|compensation|ctc)\b|\bsalary\s*expectation/i, F.expected_salary || "Negotiable"],
    [/\bcurrent\b.*\b(salary|pay|ctc)\b/i, F.current_salary || ""],
    [/\b(university|college|school|institution|institute)\b/i, F.university || "University of Bahrain"],
    [/\b(degree|qualification|education\s*level|highest)\b/i, F.degree_short || "Bachelor's degree"],
    [/\b(major|field of study|specialis|discipline|course)\b/i, "Civil Engineering"],
    [/\b(graduat|completion|end)\w*\s*(year|date)\b|\byear\s*of\s*(graduation|passing)\b/i, F.grad_year || "2021"],
    [/\bgpa\b|\bgrade\b|\bcgpa\b/i, F.gpa || "3.17"],
    [/\b(gender|sex)\b/i, F.gender || ""],
    [/\b(date of birth|dob|birth)\b/i, F.dob || ""],
    [/\bage\b/i, F.age || ""],
    [/\b(cover\s*letter|covering letter|motivation|why (do you|are you|should we)|tell us|about yourself|introduce yourself|summary|message to|additional information|comments?)\b/i, (d) => answerFor(d) || coverLetter],
    [/\b(skills?|certifications?|software)\b/i, (d) => answerFor(d) || (C.software || []).join(", ")],
    [/\b(languages?)\b/i, (C.languages || []).join(", ")],
    [/\b(headline|profile title|title)\b/i, C.headline],
    [/\bname\b/i, C.name],
  ];
  // ---- yes/no questions (radios, selects, checkboxes with Yes/No options)
  const YESNO = [
    [/\b(authori[sz]ed|legally|eligible|permitted|right)\b.*\bwork\b|\bwork\s*(permit|authori[sz]ation)\b/i, inGCC ? "Yes" : "No"],
    [/\bsponsor/i, inGCC ? "No" : "Yes"],
    [/\bvisa\b/i, inGCC ? "No" : "Yes"],
    [/\brelocat|\bwilling to (move|travel|work)|\bonsite|\bon-site|\bshift|\brotation|\bovertime|\bremote\b/i, "Yes"],
    [/\b(18|eighteen)\b|\blegal age\b|\badult\b/i, "Yes"],
    [/\bdriv(ing|er'?s?)\s*licen[cs]e\b/i, "Yes"],
    [/\bnebosh|iosh|safety\s*(cert|card)|\bhse\b/i, "Yes"],
    [/\b(bachelor|degree|engineering degree|b\.?sc)\b/i, "Yes"],
    [/\bautocad|civil ?3d|ms project|excel\b/i, "Yes"],
    [/\bcivil engineer(ing)?\b.*\b(experience|background)\b/i, "Yes"],
    [/\bsaudi council|\bsce\b/i, F.sce_registered || "No"],
    [/\bp\.?\s*eng\b|\bprofessional engineer\b|\bchartered\b|\brpeq\b|\bcpeng\b/i, "No"],
    [/\bpreviously (worked|applied|employed)|\bformer employee|\bcurrently (work|employed) (at|with|for)\b/i, "No"],
    [/\brelative|\bfamily member|\bconflict of interest|\bgovernment official\b/i, "No"],
    [/\bconvict|\bcriminal|\bfelony\b/i, "No"],
    [/\bdisabilit|\bveteran\b/i, "No"],
    [/\b(employment|non-?compete|restrictive)\b.*\bagreement/i, "No"],
    [/\bagree to\b|\bconsent\b|\backnowledge\b|\bcertify\b|\bprivacy (policy|notice)\b|\bterms (and|&) conditions\b/i, "Yes"],
    [/\bbased in\b|\bcurrently (reside|living|located)\b|\blocal candidate\b/i, inGCC && /bahrain/i.test(JOB.location || "") ? "Yes" : "No"],
    [/\bnotice\b/i, "Yes"],
  ];

  function answerFor(d) {
    for (const [rx, v] of answersRx) if (rx.test(d)) return v;
    return "";
  }
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
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
  function setValue(el, value) {
    SET.set(el, { v: value, n: (SET.get(el) || { n: 0 }).n + 1 });
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value");
    el.focus();
    if (setter && setter.set) setter.set.call(el, value); else el.value = value;
    for (const t of ["input", "change", "keyup", "blur"]) el.dispatchEvent(new Event(t, { bubbles: true }));
    el.setAttribute("data-jobagent", "1");
  }
  function chooseOption(sel, wanted) {
    const w = String(wanted).toLowerCase();
    const opts = [...sel.options];
    let o = opts.find(o => o.text.trim().toLowerCase() === w) || opts.find(o => o.text.trim().toLowerCase().startsWith(w)) || opts.find(o => o.text.toLowerCase().includes(w));
    if (!o && /^\+?\d+$/.test(w)) o = opts.find(o => o.text.replace(/\s/g, "").includes(w.replace("+", "")) || o.value.includes(w.replace("+", "")));
    if (!o) return false;
    sel.focus(); sel.value = o.value;
    for (const t of ["input", "change", "blur"]) sel.dispatchEvent(new Event(t, { bubbles: true }));
    sel.setAttribute("data-jobagent", "1");
    return true;
  }
  const isCombo = (el) => el.tagName === "INPUT" && !el.closest('[class*="iti"]') && (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" || /select__input|react-select|combobox|autocomplete/i.test((el.className || "") + " " + (el.parentElement && el.parentElement.className || "")));
  function fillCombo(el, want) {    // the app drives these with real mouse/keyboard input (frameworks ignore synthetic events here)
    el.setAttribute("data-jobagent", "1"); el.setAttribute("data-jobagent-combo", want);
  }
  function valueFor(el, d) {
    for (const [rx, v] of RULES) if (rx.test(d)) return typeof v === "function" ? v(d, el) : v;
    return null;
  }
  function yesNoFor(d) { for (const [rx, v] of YESNO) if (rx.test(d)) return v; return null; }

  function fillOnce() {
    let filled = 0;
    const seenGroups = new Set();
    for (const el of document.querySelectorAll("input, textarea, select")) {
      if (!vis(el) || el.disabled || el.readOnly) continue;
      if (el.getAttribute("data-jobagent")) {
        const prev = SET.get(el);
        if (prev && prev.n < 4 && el.tagName !== "SELECT" && !el.value) { setValue(el, prev.v); filled++; }   // re-render wiped it
        continue;
      }
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["hidden", "submit", "button", "file", "password", "search", "image", "reset"].includes(type)) continue;
      let d = describe(el);
      if (type === "radio" || (type === "checkbox" && el.name && document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length > 1)) {
        const fs = el.closest("fieldset,[role=group],[role=radiogroup]");
        const lg = fs ? (fs.querySelector("legend") || (fs.getAttribute("aria-labelledby") ? document.getElementById(fs.getAttribute("aria-labelledby")) : null)) : null;
        if (lg) d = txt(lg) + " | " + d; else if (fs && fs.getAttribute("aria-label")) d = fs.getAttribute("aria-label") + " | " + d;
        else { let p = (el.closest("label") || el).parentElement, q = ""; for (let i = 0; p && i < 4 && !q; i++, p = p.parentElement) { for (let s = p.previousElementSibling; s; s = s.previousElementSibling) { if (s.querySelector("input,select,textarea")) break; const t = txt(s); if (t && t.length < 220) { q = t; break; } } } if (q) d = q + " | " + d; }
      }
      if (!d) continue;
      if (type === "radio" || type === "checkbox") {
        const key = el.name || d; if (seenGroups.has(key)) continue;
        const group = el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] : [el];
        if (group.some(g => g.checked)) { seenGroups.add(key); continue; }
        const labels = group.map(g => ({ g, t: (txt(g.closest("label")) || txt(document.querySelector(`label[for="${CSS.escape(g.id || "")}"]`)) || "").toLowerCase() }));
        const q = d + " " + labels.map(l => l.t).join(" ");
        let want = yesNoFor(q);
        if (type === "checkbox" && group.length === 1) {
          if (/\bagree|\bconsent|\bconfirm|\backnowledge|\bcertify|\bprivacy|\bterms|\baccept\b|\bauthori[sz]e\b/i.test(q)) { el.click(); el.setAttribute("data-jobagent", "1"); filled++; }
          seenGroups.add(key); continue;
        }
        if (want) {
          const wl = want.toLowerCase();
          let pick = labels.find(l => l.t === wl) || labels.find(l => l.t.startsWith(wl)) || labels.find(l => l.t.includes(wl));
          if (!pick) { // non yes/no groups: country / degree etc.
            const v = valueFor(el, d); if (v) pick = labels.find(l => l.t.includes(String(v).toLowerCase()));
          }
          if (pick) { pick.g.click(); pick.g.setAttribute("data-jobagent", "1"); filled++; }
        } else {
          const v = valueFor(el, d); const pick = v ? labels.find(l => l.t.includes(String(v).toLowerCase())) : null;
          if (pick) { pick.g.click(); pick.g.setAttribute("data-jobagent", "1"); filled++; }
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
  let total = 0, bar;
  function bar_() {
    if (bar && document.body.contains(bar)) return bar;
    bar = document.createElement("div");
    bar.id = "jobagent-bar";
    bar.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#111827;color:#fff;font:13px/1.4 system-ui,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.35);display:flex;gap:10px;align-items:center";
    bar.innerHTML = `<b>JobAgent</b><span id="ja-msg">ready</span><button id="ja-fill" style="font:inherit;border:0;border-radius:6px;padding:5px 9px;background:#2563eb;color:#fff;cursor:pointer">Fill again</button><button id="ja-done" style="font:inherit;border:0;border-radius:6px;padding:5px 9px;background:#16a34a;color:#fff;cursor:pointer">I submitted ✓</button><button id="ja-x" style="font:inherit;border:0;background:none;color:#9ca3af;cursor:pointer">×</button>`;
    document.body.appendChild(bar);
    bar.querySelector("#ja-fill").onclick = () => run(true);
    bar.querySelector("#ja-done").onclick = () => mark("Applied");
    bar.querySelector("#ja-x").onclick = () => bar.remove();
    return bar;
  }
  function say(m) { try { bar_().querySelector("#ja-msg").textContent = m; } catch (_) {} }
  function mark(status) {
    say("saving…");
    fetch(CTX.app + "/api/mark", { method: "POST", mode: "cors", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: JOB.url, status }) })
      .then(() => say("marked as " + status + " in the app ✓")).catch(() => say("could not reach the app - mark it there"));
    window.__jobagent.submitted = true;
  }
  function run(force) {
    if (!document.body) return { filled: 0 };
    const n = fillOnce(); total += n;
    const sub = SUBMITTED.test((document.body.innerText || "").slice(0, 20000));
    if (sub && !window.__jobagent.submitted) { mark("Applied"); }
    say(total ? `filled ${total} field${total > 1 ? "s" : ""} - check, then press the site's button` : "no fields yet - open the apply form");
    return { filled: n, total, submitted: sub };
  }
  window.__jobagent = { run, submitted: false };
  const mo = new MutationObserver(() => { clearTimeout(window.__jobagent._t); window.__jobagent._t = setTimeout(() => run(false), 700); });
  const start = () => { bar_(); setTimeout(() => run(false), 1200); setTimeout(() => run(false), 4000); mo.observe(document.body, { childList: true, subtree: true }); };
  if (document.readyState !== "complete") window.addEventListener("load", start); else start();
})();
