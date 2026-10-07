#!/usr/bin/env python3
"""Fill a job application form for you and stop before Submit.

  python apply_assist.py <job id>                 # opens a browser window, fills the form, waits for you
  python apply_assist.py <job id> --dry-run       # read the form and print planned answers, fill nothing
  python apply_assist.py <job id> --url <form url>   # when the tracker's URL is not the form itself

What it does: reads every field (text, dropdown, radio, checkbox, file), asks Claude for answers grounded in
profile.yaml (never invented), fills them in, attaches your resume/cover letter from out/<job>/, and prints
a report of what it filled, what it left for you, and what deserves a second look.
What it never does: click Submit, solve captchas, or answer legal certifications, consents, signatures,
salary, start date, or pronouns. Essays are drafted, fact-checked and flagged for your review.
You then solve any captcha, review the page, and click Submit yourself. Close the tab/window when done.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

from common import ROOT, SONNET, ask_json, db, load_profile, profile_text, usage_report

EXTRACT_JS = r"""
() => {
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const vis = e => {
    if (!(e.offsetParent || e.getClientRects().length)) return false;
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return r.width > 3 && r.height > 3 && cs.opacity !== '0' && cs.visibility !== 'hidden';
  };
  const esc = s => CSS.escape(s);
  const label = el => {
    let t = '';
    if (el.labels && el.labels.length) t = txt(el.labels[0].innerText);
    if (!t) t = txt(el.getAttribute('aria-label'));
    if (!t && el.getAttribute('aria-labelledby'))
      t = txt(el.getAttribute('aria-labelledby').split(' ').map(i => (document.getElementById(i) || {}).innerText || '').join(' '));
    if (!t) {
      for (let c = el.parentElement, d = 0; c && d < 7 && !t; c = c.parentElement, d++) {
        const names = new Set([...c.querySelectorAll('input,textarea,select')].filter(x => !['hidden','submit','button'].includes(x.type)).map(x => x.name || x.id));
        if (names.size > 1) break;
        const l = c.querySelector('legend,label,[class*=label],[class*=Label]');
        if (l && txt(l.innerText) && l !== el) t = txt(l.innerText);
      }
    }
    return t.slice(0, 320);
  };
  const selFor = (el, i) => {
    if (el.id && document.querySelectorAll('#' + esc(el.id)).length === 1) return '#' + esc(el.id);
    el.setAttribute('data-aa', 'f' + i);
    return '[data-aa="f' + i + '"]';
  };
  const fields = [], radios = {}, cbs = {};
  document.querySelectorAll('input,textarea,select').forEach((el, i) => {
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'search', 'image', 'reset'].includes(type)) return;
    if ((el.id || '').startsWith('g-recaptcha') || (el.name || '').startsWith('g-recaptcha')) return;
    if (type === 'file') {
      let ft = '';
      for (let c = el.parentElement, d = 0; c && d < 6 && !ft; c = c.parentElement, d++) {
        const m = txt(c.innerText).match(/cover letter|resume\/cv|resume|curriculum vitae/i);
        if (m) ft = m[0];
      }
      fields.push({kind: 'file', sel: selFor(el, i), question: (ft ? ft + ' upload' : (label(el) || 'file upload')), required: !!el.required});
      return;
    }
    if (!vis(el) && type !== 'radio' && type !== 'checkbox') return;
    const req = !!el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio') {
      const g = el.name || ('r' + i);
      radios[g] = radios[g] || {kind: 'radio', name: g, options: [], sels: [], required: req,
        question: txt(((el.closest('fieldset') || {}).querySelector ? ((el.closest('fieldset').querySelector('legend') || {}).innerText) : '') || label(el))};
      const ol = txt((el.labels && el.labels[0] ? el.labels[0].innerText : '') || el.getAttribute('aria-label') || el.value);
      radios[g].options.push(ol); radios[g].sels.push(selFor(el, i));
      return;
    }
    if (type === 'checkbox') {
      const c = el.closest('fieldset,[role=group],[class*=checkbox-group],[class*=question]');
      const key = c ? (c.id || c.className || 'grp').toString().slice(0, 60) + '|' + (el.name || '') : 'solo' + i;
      cbs[key] = cbs[key] || {kind: 'checkbox', options: [], sels: [], required: req,
        question: c ? txt((c.querySelector('legend,label,[class*=label]') || {}).innerText || '') : ''};
      const ol = txt((el.labels && el.labels[0] ? el.labels[0].innerText : '') || el.getAttribute('aria-label') || el.name || el.value);
      cbs[key].options.push(ol); cbs[key].sels.push(selFor(el, i));
      return;
    }
    if (el.tagName === 'SELECT') {
      fields.push({kind: 'select', sel: selFor(el, i), question: label(el), required: req,
        options: [...el.options].map(o => txt(o.text)).filter(t => t && !/^select/i.test(t) && t !== '--')});
      return;
    }
    const isCombo = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || /select__input/.test(el.className);
    fields.push({kind: isCombo ? 'combobox' : (el.tagName === 'TEXTAREA' ? 'textarea' : 'text'), input_type: type,
      sel: selFor(el, i), question: label(el), placeholder: el.placeholder || '', required: req});
  });
  // option groups often have no legend: find the heading that sits above the group
  const groupHeading = (sel, options) => {
    const el = document.querySelector(sel);
    if (!el) return '';
    const opts = new Set(options.map(o => o.toLowerCase()));
    for (let c = el.parentElement, d = 0; c && d < 7; c = c.parentElement, d++) {
      const prev = c.previousElementSibling;
      const t = prev ? txt(prev.innerText) : '';
      if (t && t.length < 260 && !opts.has(t.toLowerCase()) && !/^(yes|no)$/i.test(t)) return t;
      const lab = c.querySelector(':scope > legend, :scope > label, :scope > [class*=label], :scope > [class*=Label], :scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > p');
      const lt = lab ? txt(lab.innerText) : '';
      if (lt && lt.length < 260 && !opts.has(lt.toLowerCase())) return lt;
    }
    return '';
  };
  const fixQ = g => {
    const q = (g.question || '').toLowerCase();
    if (!g.question || g.options.some(o => o.toLowerCase() === q)) g.question = groupHeading(g.sels[0], g.options) || g.question;
  };
  Object.values(radios).forEach(r => { fixQ(r); fields.push(r); });
  Object.values(cbs).forEach(c => { if (!c.question && c.options.length === 1) c.question = c.options[0]; fixQ(c); fields.push(c); });
  // Ashby-style Yes/No button pairs
  let yn = 0;
  document.querySelectorAll('div,fieldset').forEach(d => {
    const bs = [...d.children].filter(x => x.tagName === 'BUTTON');
    if (bs.length === 2 && txt(bs[0].innerText) === 'Yes' && txt(bs[1].innerText) === 'No') {
      const holder = d.closest('[class*=field],[class*=question],[class*=Field],[class*=Question]') || d.parentElement;
      const l = holder && holder.querySelector('label,legend,[class*=label],[class*=Label]');
      d.setAttribute('data-aa-yn', 'y' + yn);
      fields.push({kind: 'yesno', sel: '[data-aa-yn="y' + yn + '"]', question: txt(l ? l.innerText : holder.innerText).slice(0, 320), options: ['Yes', 'No'], required: true});
      yn++;
    }
  });
  return fields;
}
"""

SYSTEM = """You fill job application forms for one candidate. You get PROFILE (facts and standing rules), the JOB, and
FIELDS extracted from the form. Return ONLY JSON:
{"answers": {"<key>": {"value": <see below>, "confidence": "high"|"medium"|"low", "note": "<why, brief>"}},
 "skipped": {"<key>": "<reason>"}}
Value types: text/textarea -> string; select/radio/combobox/yesno -> EXACTLY one string from that field's options
(for a combobox without options, the text to type); checkbox -> list of exact option strings to tick
(a single consent-style checkbox is never ticked); file -> "resume" or "cover_letter".
Hard rules:
1. Use ONLY facts in PROFILE. Never invent employers, skills, numbers, dates or experiences. If unsure, skip.
2. Follow PROFILE.application_defaults exactly: work authorization rules, voluntary demographics (only when a
   matching question exists; if no matching option, choose the form's decline option), education/employment
   entries, postal code. If a needed default is empty (e.g. state_of_residence), SKIP it.
2b. Demographics come ONLY from the stored values. Never infer race, ethnicity, gender or anything else from the
   candidate's name, education, or where they studied. Choose the option that equals or contains the stored
   value; if none does, choose the form's decline option.
2c. If a question asks whether the candidate already lives near / can commute to an office located in
   PROFILE.application_defaults.residence_metro, answer that they already reside nearby (never the "willing to
   relocate" option). For offices elsewhere, skip it.
3. NEVER answer: legal certifications/signatures/"I agree"/privacy/terms consents, SMS or AI-notetaker or
   data-processing consents, salary expectations, start date/notice period, pronouns, commitments about
   in-office days / onsite schedules / relocation / travel / availability, "may we contact your
   current employer", why-relocate or visa specifics beyond the rules. Skip them with a short reason.
4. Names, locations, GPA and years of experience: use PROFILE.application_defaults.identity_for_forms exactly
   (full/legal name, first, last, preferred first name, location text, city autocomplete text, GPA text, years).
   Email, phone, LinkedIn and GitHub come from PROFILE.person; website/portfolio -> the GitHub URL. Country: the
   country in PROFILE.person.location.
5. "How did you hear about us": pick the closest of company website/careers page, job board, or Other; confidence low.
6. Essays (textarea asking about motivation, experience, projects): write 80-140 words, first person, plain, using
   only PROFILE facts and what the JOB says about the company; set note "ESSAY - review before submitting".
7. GPA and years of experience: from identity_for_forms (free text vs numeric-only fields).
8. Resume upload -> "resume". Cover letter upload -> "cover_letter". Other uploads -> skip.
9. Do not guess when a question is ambiguous: skip and say why."""


def slug_of(company, title):
    return re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")[:80]


def application_url(job_id, source, company, url):
    if source == "greenhouse":
        return f"https://job-boards.greenhouse.io/embed/job_app?for={company}&token={job_id.split(':', 1)[1]}"
    if "lever.co" in url and not url.rstrip("/").endswith("/apply"):
        return url.rstrip("/") + "/apply"
    if "ashbyhq.com" in url and not url.rstrip("/").endswith("/application"):
        return url.rstrip("/") + "/application"
    return url


GENERIC_Q = {"", "type your response", "type here...", "attach", "file upload"}


DEMO = [(r"hispanic|latino", "hispanic_latino"), (r"race|ethnic", "race"), (r"gender|sex\b", "gender"),
        (r"veteran", "veteran_status"), (r"disab", "disability_status")]
DECLINE = re.compile(r"decline|prefer not|do not wish|don.t wish|choose not", re.I)


def strict_option(want, options):
    """Exact, or prefix ('Asian' -> 'Asian (Not Hispanic or Latino)'). Never sub-categories or substrings."""
    w = re.sub(r"\s+", " ", str(want)).strip().lower()
    for o in options:
        if re.sub(r"\s+", " ", o).strip().lower() == w:
            return o
    for o in options:
        if re.sub(r"\s+", " ", o).strip().lower().startswith(w):
            return o
    return None


def demographic_answer(f, stored):
    """None = not a demographic question; "" = it is, but no safe option; else the option to pick."""
    q = (f.get("question") or "").lower()
    for pat, key in DEMO:
        if re.search(pat, q):
            opts = f.get("options") or []
            if not opts or key not in stored:
                return ""
            return strict_option(stored[key], opts) or next((o for o in opts if DECLINE.search(o)), "")
    return None


def dedupe(fields):
    """Custom dropdowns render a hidden twin input; keep the richest widget per question."""
    rank = {"select": 3, "yesno": 3, "radio": 3, "combobox": 2, "textarea": 1, "text": 0}
    out, seen = [], {}
    for f in fields:
        q = re.sub(r"[\s*]+", " ", f.get("question") or "").strip().lower()
        if q not in GENERIC_Q and f["kind"] in rank and f["kind"] != "file":
            if q in seen:
                prev = seen[q]
                score = lambda x: rank[x["kind"]] + (0.5 if x.get("input_type") == "tel" else 0) + (0.2 if str(x.get("sel", "")).startswith("#") else 0)
                if score(f) > score(prev):
                    out[out.index(prev)] = f
                    seen[q] = f
                continue
            seen[q] = f
        out.append(f)
    return out


def fuzzy_option(value, options):
    """Exact match first, then prefix, then whole-word. Never a bare substring ('male' must not match 'Female')."""
    v = re.sub(r"\s+", " ", str(value)).strip().lower()
    if not v:
        return None
    norm = [(o, re.sub(r"\s+", " ", o).strip().lower()) for o in options]
    for o, n in norm:
        if n == v:
            return o
    for o, n in norm:
        if n.startswith(v) or v.startswith(n) and len(n) > 3:
            return o
    pat = re.compile(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])")
    for o, n in norm:
        if pat.search(n):
            return o
    return None


def read_combobox_options(page, sel):
    try:
        loc = page.locator(sel).first
        loc.click(timeout=3000)
        page.wait_for_timeout(350)
        opts = page.locator("[role=option]:visible").all_inner_texts()
        page.keyboard.press("Escape")
        opts = [re.sub(r"\s+", " ", o).strip() for o in opts if o.strip()]
        return opts if 0 < len(opts) <= 60 else None
    except Exception:
        return None


def fill_field(page, f, value, files):
    kind = f["kind"]
    if kind in ("text", "textarea"):
        page.locator(f["sel"]).first.fill(str(value), timeout=4000)
    elif kind == "select":
        page.locator(f["sel"]).first.select_option(label=value, timeout=4000)
    elif kind == "radio":
        i = f["options"].index(value)
        page.locator(f["sels"][i]).first.check(force=True, timeout=4000)
    elif kind == "checkbox":
        for v in value:
            page.locator(f["sels"][f["options"].index(v)]).first.check(force=True, timeout=4000)
    elif kind == "yesno":
        page.locator(f["sel"]).locator(f"button:text-is('{value}')").first.click(timeout=4000)
    elif kind == "file":
        page.locator(f["sel"]).first.set_input_files(str(files[value]), timeout=8000)
    elif kind == "combobox":
        loc = page.locator(f["sel"]).first
        loc.click(timeout=3000)
        query = str(value)[:30] if f.get("options") else re.split(r"[,(]", str(value))[0].strip()[:24]
        page.keyboard.type(query, delay=40)
        texts = []
        for _ in range(8):
            page.wait_for_timeout(400)
            texts = [re.sub(r"\s+", " ", t).strip() for t in page.locator("[role=option]:visible").all_inner_texts()]
            if texts:
                break
        chosen = fuzzy_option(value, texts) or (next((t for t in texts if t.lower().startswith(query.lower())), None)
                                                if not f.get("options") else None)
        if chosen is None:
            page.keyboard.press("Escape")
            raise RuntimeError(f"no safe option match for '{value}' among {texts[:6]}")
        page.locator("[role=option]:visible").nth(texts.index(chosen)).click(timeout=3000)
        f["chosen"] = chosen
    return True


def verify(page, f, value):
    """Read back what the page now shows. True/False, or None when it cannot be checked."""
    try:
        if f["kind"] in ("text", "textarea"):
            return page.locator(f["sel"]).first.input_value() != ""
        if f["kind"] == "select":
            return page.locator(f["sel"]).first.evaluate("e => e.options[e.selectedIndex].text").strip().lower() == str(value).strip().lower()
        if f["kind"] == "combobox":
            if re.match(r"country", (f.get("question") or "").strip(), re.I):
                return None
            want = (f.get("chosen") or str(value)).lower()
            box = page.locator(f["sel"]).first.locator("xpath=ancestor::*[self::div][3]").inner_text().lower()
            return re.search(r"(?<![a-z0-9])" + re.escape(want) + r"(?![a-z0-9])", box) is not None
        return True
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_id")
    ap.add_argument("--url")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--manual", action="store_true", help="print a paste sheet and open the form in your normal browser (for sites that reject automated browsers)")
    ap.add_argument("--headless", action="store_true", help="fill without a visible window, save a screenshot, exit")
    a = ap.parse_args()
    if a.manual:
        a.dry_run = True

    conn = db()
    row = conn.execute("SELECT source, company, title, location, url, description FROM jobs WHERE id=?", (a.job_id,)).fetchone()
    if not row:
        raise SystemExit(f"no job {a.job_id}")
    source, company, title, location, url, description = row
    folder = ROOT / "out" / slug_of(company, title)
    files = {"resume": folder / "resume.pdf", "cover_letter": folder / "cover_letter.pdf"}
    if not files["resume"].exists():
        import subprocess
        print("no resume/cover letter yet; generating them now...")
        r = subprocess.run([sys.executable, "tailor.py", a.job_id], cwd=ROOT, capture_output=True, text=True)
        print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:])
        if not files["resume"].exists():
            raise SystemExit("could not generate the resume; run tailor.py manually and check the error")
    target = a.url or application_url(a.job_id, source, company, url)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=a.dry_run or a.headless)
        ctx = browser.new_context(viewport={"width": 1100, "height": 900})
        page = ctx.new_page()
        print(f"opening {target}")
        page.goto(target, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        try:
            page.wait_for_selector("input:visible, textarea:visible, select:visible", timeout=15000)
        except Exception:
            pass
        scope = page
        fields = scope.evaluate(EXTRACT_JS)
        if not fields:
            for fr in page.frames:
                try:
                    fields = fr.evaluate(EXTRACT_JS)
                except Exception:
                    fields = []
                if fields:
                    scope = fr
                    break
        if not fields:
            print("no form fields found. If there is an Apply button, click it, then re-run with --url of the form.")
            page.wait_for_event("close", timeout=0)
            return
        fields = dedupe(fields)
        for i, f in enumerate(fields):
            f["key"] = f"k{i}"
            if f["kind"] == "combobox":
                f["options"] = read_combobox_options(page, f["sel"])
        listing = [{k: v for k, v in f.items() if k not in ("sel", "sels", "name")} for f in fields]
        print(f"found {len(fields)} fields; asking Claude for answers...")

        avail = [n for n, p_ in files.items() if p_.exists()]
        user = (f"PROFILE:\n{profile_text()}\n\nFILES AVAILABLE TO ATTACH: {avail} (a cover letter upload -> \"cover_letter\")\n\n"
                f"JOB: {company} - {title} ({location})\n{description[:5000]}\n\n"
                f"FIELDS:\n{json.dumps(listing, indent=1)}")
        plan = ask_json(SONNET, SYSTEM, user, max_tokens=6000, retries=3)
        answers, skipped = plan.get("answers", {}), dict(plan.get("skipped", {}))

        # fact-check any drafted essay; if it fails, leave it for the user
        from tailor import check_facts
        for f in fields:
            ans = answers.get(f["key"])
            if ans and f["kind"] == "textarea" and len(str(ans.get("value", ""))) > 200:
                bad = check_facts({"summary": "", "cover_letter": [str(ans["value"])]})
                if bad:
                    answers.pop(f["key"])
                    skipped[f["key"]] = "essay draft failed the fact-check (" + "; ".join(bad)[:160] + ")"

        defaults = load_profile().get("application_defaults") or {}
        stored = defaults.get("demographics_voluntary") or {}
        if not defaults.get("state_of_residence"):
            for f in fields:
                q = (f.get("question") or "").lower()
                if re.search(r"\b(state|province|region)\b", q) and re.search(r"resid|live|locat|currently|based", q):
                    if answers.pop(f["key"], None):
                        skipped[f["key"]] = "state of residence left for you (NY vs NJ), not guessed"
        # Code-level backstop: the model has repeatedly answered relocation commitments anyway despite the
        # prompt rule against it (e.g. "willing to relocate to the Bay Area?" -> Yes at low confidence).
        # Force-skip any question that names relocation and isn't clearly about the candidate's own metro.
        metro = (defaults.get("residence_metro") or "").lower()
        for f in fields:
            q = (f.get("question") or "").lower()
            if "relocat" in q and not (metro and metro in q):
                if answers.pop(f["key"], None):
                    skipped[f["key"]] = "relocation commitment - never auto-answer, left for you regardless of model output"
        for f in fields:
            if f["kind"] in ("select", "radio", "combobox", "yesno", "checkbox"):
                d = demographic_answer(f, stored)
                if d is None:
                    continue
                if d:
                    answers[f["key"]] = {"value": [d] if f["kind"] == "checkbox" else d, "confidence": "high",
                                         "note": "stored demographic answer"}
                    skipped.pop(f["key"], None)
                else:
                    answers.pop(f["key"], None)
                    skipped[f["key"]] = "demographic question with no exact or decline option; left for you"

        report = {"filled": [], "left": [], "review": []}
        for f in fields:
            k, q = f["key"], (f.get("question") or f.get("placeholder") or f["kind"])[:110]
            if k not in answers:
                report["left"].append((q, skipped.get(k, "no answer given"), f.get("required")))
                continue
            val, conf = answers[k]["value"], answers[k].get("confidence", "medium")
            if f["kind"] in ("select", "radio", "yesno"):
                m = fuzzy_option(val, f.get("options", []))
                if not m:
                    report["left"].append((q, f"answer '{val}' not among the options", f.get("required")))
                    continue
                val = m
            if f["kind"] == "checkbox":
                vals = [fuzzy_option(v, f["options"]) for v in (val if isinstance(val, list) else [val])]
                val = [v for v in vals if v]
                if not val:
                    report["left"].append((q, "no matching option", f.get("required")))
                    continue
            if f["kind"] == "file" and val not in files:
                report["left"].append((q, "unknown file type", f.get("required")))
                continue
            if a.dry_run:
                # In --manual mode this is the ONLY record of what to type - never truncate it, or
                # the user ends up pasting a cut-off essay. Plain --dry-run previews can stay short.
                shown = str(val) if a.manual else str(val)[:90]
                report["filled"].append((q, shown, conf))
                continue
            try:
                try:
                    fill_field(scope, f, val, files)
                except Exception:
                    for nf in scope.evaluate(EXTRACT_JS):
                        if nf["kind"] == f["kind"] and nf.get("question") == f.get("question"):
                            f["sel"], f["sels"] = nf.get("sel"), nf.get("sels")
                            break
                    fill_field(scope, f, val, files)
                ok = verify(scope, f, val)
                tag = "" if ok in (True, None) else " (could not verify - check it)"
                report["filled"].append((q, str(val)[:90] + tag, conf))
                if conf == "low" or "ESSAY" in answers[k].get("note", "") or tag:
                    report["review"].append((q, answers[k].get("note", "") + tag))
            except Exception as e:
                report["left"].append((q, f"could not fill: {str(e)[:80]}", f.get("required")))

        lines = [f"{company} - {title}", target, ""]
        lines.append(("VALUES TO PASTE" if a.manual else "PLANNED (dry run)" if a.dry_run else "FILLED") + f" ({len(report['filled'])}):")
        lines += [f"  + {q}  ->  {v}   [{c}]" for q, v, c in report["filled"]]
        lines.append(f"\nLEFT FOR YOU ({len(report['left'])}):")
        lines += [f"  - {'(REQUIRED) ' if req else ''}{q}: {why}" for q, why, req in report["left"]]
        if report["review"]:
            lines.append("\nPLEASE REVIEW:")
            lines += [f"  ! {q}: {n}" for q, n in report["review"]]
        lines += ["", "Solve any captcha, review the page, then click Submit yourself. Close the tab when done.",
                  usage_report()]
        text = "\n".join(lines)
        print("\n" + text)
        (folder / "apply_report.txt").write_text(text + "\n")

        if a.dry_run:
            browser.close()
            if a.manual:
                import subprocess
                subprocess.run(["open", target])
                print(f"opened {target} in your default browser; report saved to {folder / 'apply_report.txt'}")
            return
        if a.headless:
            page.screenshot(path=str(folder / "apply_filled.png"), full_page=True)
            print(f"screenshot: {folder / 'apply_filled.png'}")
            browser.close()
            return
        watch_for_submission(page, a.job_id, company, title)


CONFIRMED_RE = re.compile(
    r"thank you for (applying|your application|your interest)|thanks for applying|application (has been |was )?(successfully )?(submitted|received)"
    r"|we(?:'|’)ve received your application|we have received your application|your application is (in|on its way)", re.I)
CONFIRMED_URL_RE = re.compile(r"confirmation|/thanks|thank-you|submitted|application-complete", re.I)


def page_confirms(page) -> bool:
    if CONFIRMED_URL_RE.search(page.url):
        return True
    for fr in page.frames:
        try:
            if CONFIRMED_RE.search(fr.evaluate("document.body ? document.body.innerText.slice(0, 4000) : ''")):
                return True
        except Exception:
            continue
    return False


def watch_for_submission(page, job_id, company, title):
    """Keep the window open for review; when the confirmation page appears, mark the job applied."""
    print("watching for the confirmation page; this job is marked applied automatically once you submit.")
    while not page.is_closed():
        try:
            if page_confirms(page):
                conn = db()
                conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
                conn.commit()
                print(f"submission detected: marked applied - {company} - {title} ({job_id})")
                page.wait_for_timeout(2500)
                return
            page.wait_for_timeout(2000)
        except Exception:
            return
    print(f"window closed without a confirmation page; {job_id} left as is")


if __name__ == "__main__":
    main()
