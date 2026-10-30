"""Generic application-form filler: LinkedIn Easy Apply steps, Indeed/Glassdoor
smartapply, Unstop, and company forms (Greenhouse, Lever, Ashby).

It reads every visible field inside `root` - label, hint text, validation error,
options - fills the ones still empty from your profile, saved answers or the
model, uploads the resume, and never guesses an unlabelled or optional free-text
field.
"""
from __future__ import annotations

import itertools
import re

from . import answers, human, llm
from .config import PROFILE, resume_path

_step = itertools.count()

FIELDS_JS = r"""
({root, prefix}) => {
  const scope = document.querySelector(root) || document.body;
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                      return (r.width > 0 || r.height > 0) && s.visibility !== 'hidden' && s.display !== 'none'; };
  const t = el => (el && el.innerText || '').trim();
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (t(l)) return t(l); }
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const s = lb.split(' ').map(i => t(document.getElementById(i))).join(' ').trim(); if (s) return s; }
    if (t(el.closest('label'))) return t(el.closest('label'));
    const box = el.closest('.fb-dash-form-element, .jobs-easy-apply-form-element, .field, .application-question, [class*="field"], [class*="question"], li');
    if (box) { const l = box.querySelector('label, legend, .application-label, [class*="label"]'); if (t(l)) return t(l); }
    return el.placeholder || el.name || '';
  };
  const described = el => (el.getAttribute('aria-describedby') || '').split(' ').map(i => t(document.getElementById(i))).join(' ').replace(/\d+\/\d+|\d+ of \d+ characters/g, '').trim();
  const hintOf = el => {
    const box = el.closest('[componentkey], .fb-dash-form-element, [class*="form-element"], [class*="question"]') || el.parentElement?.parentElement;
    const ps = box ? [...(box.parentElement || box).querySelectorAll(':scope > p, :scope > div > p')].map(t).filter(Boolean) : [];
    return [...new Set([...ps, described(el)])].join(' | ').slice(0, 300);
  };
  const errorOf = el => {
    const fs = el.closest('fieldset');
    const ids = ((fs && fs.getAttribute('aria-describedby')) || el.getAttribute('aria-describedby') || '').split(' ');
    const txt = ids.map(i => document.getElementById(i)).filter(e => e && /error/i.test(e.id + ' ' + e.className)).map(t).join(' ');
    return (el.getAttribute('aria-invalid') === 'true' || txt) ? (txt || 'invalid value') : '';
  };
  const optionText = el => {
    let n = el.parentElement;
    for (let k = 0; k < 5 && n; k++, n = n.parentElement) {
      if (n.querySelectorAll('input[type="radio"], input[type="checkbox"]').length > 1) break;
      if (t(n)) return t(n);
    }
    return el.value;
  };
  const nearText = el => {
    const parts = [];
    for (let n = el, k = 0; n && n !== scope && k < 6 && parts.join(' ').length < 60; n = n.parentElement, k++) {
      for (let s = n.previousElementSibling; s && parts.join(' ').length < 60; s = s.previousElementSibling) {
        if (s.querySelector && s.querySelector('input, select, textarea')) { if (parts.length) break; else continue; }
        const x = t(s); if (x) { parts.unshift(x.split('\n').slice(-2).join(' ')); break; }
      }
    }
    return parts.join(' - ').slice(0, 100);
  };
  const out = [], groups = {};
  let i = 0;
  for (const el of scope.querySelectorAll('input, textarea, select')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'search', 'image', 'reset'].includes(type)) continue;
    if (type !== 'file' && type !== 'radio' && type !== 'checkbox' && !vis(el)) continue;
    const idx = prefix + (i++);
    el.setAttribute('data-jd', idx);
    let label = labelOf(el);
    if (!label || label === el.name) label = nearText(el);
    const required = el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label) || /required/i.test(label);
    if (type === 'radio' || type === 'checkbox') {
      const lab = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest('label');
      if (!vis(el) && !(lab && vis(lab))) continue;
      const opt = t(lab) || optionText(el);
      const key = el.name || idx;
      if (!groups[key]) {
        const fs = el.closest('fieldset');
        const q = t(fs && fs.querySelector('legend')) || el.getAttribute('aria-label') ||
                  t(el.closest('[class*="question"], [class*="field"], [class*="form-element"]')?.querySelector('legend, label, [class*="label"]')) || opt;
        groups[key] = {kind: type, label: q, required: type === 'radio' || required || !!(fs && /required|\*/i.test(fs.innerText)),
                       options: [], idxs: [], checked: false, error: errorOf(el), hint: ''};
        out.push(groups[key]);
      }
      groups[key].options.push(opt);
      groups[key].idxs.push(idx);
      if (el.checked) groups[key].checked = true;
      continue;
    }
    let value = el.value || '';
    let options;
    if (el.tagName === 'SELECT') {
      options = [...el.options].map(o => o.text.trim()).filter(x => x && !/^(select|choose|--|please select)/i.test(x));
      const sel = el.options[el.selectedIndex];
      value = sel && !/^(select|choose|--|please select)/i.test(sel.text.trim()) && sel.value !== '' ? sel.text.trim() : '';
    }
    out.push({kind: el.tagName === 'SELECT' ? 'select' : (el.getAttribute('role') === 'combobox' ? 'combobox' : type),
              label, required, idx, value, options, has_file: type === 'file' ? el.files.length > 0 : undefined,
              hint: type === 'file' ? '' : hintOf(el), error: errorOf(el)});
  }
  return {fields: out, text: scope.innerText.slice(0, 5000)};
}
"""


def _standard_map() -> list[tuple[str, object]]:
    p = PROFILE.get("personal") or {}
    e = PROFILE.get("experience") or {}
    ed = PROFILE.get("education") or {}
    return [
        (r"first\s*name|given name", p.get("first_name")),
        (r"last\s*name|surname|family name", p.get("last_name")),
        (r"^\s*(full\s*)?name\b|your name", p.get("full_name")),
        (r"e-?mail", p.get("email")),
        (r"mobile phone number|phone number|^\s*phone|mobile|contact number", p.get("phone_digits")),
        (r"linkedin", p.get("linkedin_url")),
        (r"github", p.get("github_url")),
        (r"portfolio|personal website|website", p.get("portfolio_url") or p.get("github_url")),
        (r"current (location|city)|^\s*location|^\s*city", p.get("city")),
        (r"current (company|employer)", e.get("current_company")),
        (r"current (title|role|designation)", e.get("current_title")),
        (r"college|university|school", ed.get("college")),
        (r"^\s*degree", ed.get("degree")),
    ]


def standard_answer(label: str):
    for pattern, value in _standard_map():
        if value and re.search(pattern, label, re.I):
            return str(value)
    return None


def fill(page, job: dict, sess, root: str = "body") -> tuple[bool, str | None]:
    """Fill every visible, still-empty field inside root.
    Returns (ok, question_key) - a key when you must answer something first."""
    data = page.evaluate(FIELDS_JS, {"root": root, "prefix": f"s{next(_step)}_"})
    fields, scope_text = data["fields"], data["text"]
    resume = sess.resume or resume_path()
    cover = None
    for f in fields:
        label = re.sub(r"\s+", " ", f["label"] or "").strip(" *")
        label = re.sub(r"\s*(required|\(required\))\s*$", "", label, flags=re.I)
        kind = f["kind"]
        loc = page.locator(f'[data-jd="{f.get("idx")}"]') if f.get("idx") else None

        if kind == "file":
            if re.search(r"cover", label, re.I) or f.get("has_file") or resume.name in scope_text:
                continue                    # resume already attached / not a resume field
            if resume.exists():
                loc.set_input_files(str(resume))
                sess.qa.append({"q": label or "resume", "a": resume.name, "from": "file"})
                human.pause(1.5, 3)
            continue
        err = f.get("error") or ""
        if kind in ("radio", "checkbox") and f.get("checked") and not err:
            continue
        if kind not in ("radio", "checkbox") and f.get("value") and not err:
            continue                        # already filled (site profile / earlier answer)
        if not label:
            continue                        # cannot tell what this is - never guess
        hint = f.get("hint") or ""
        if hint.lower().startswith(label.lower()):
            hint = hint[len(label):].strip(" |*")

        value = standard_answer(label) if kind not in ("radio", "checkbox", "select") and not err else None
        if value is not None:
            sess.qa.append({"q": label, "a": value, "from": "profile"})
        if value is None and kind == "textarea" and re.search(
                r"cover letter|why .*(join|interested)|additional information|message to", label, re.I):
            cover = cover or llm.cover_note(job)
            value = cover or None
            if value:
                sess.qa.append({"q": label, "a": value[:300], "from": "cover note"})
        if value is None:
            if not f["required"] and kind in ("text", "textarea", "url", "email", "tel"):
                continue                    # do not guess optional free-text fields
            if kind == "checkbox" and not f["required"] and len(f["options"]) == 1:
                continue                    # optional single checkbox ("follow company")
            value, key = answers.resolve(sess, label, kind, f.get("options"), job, hint=hint, error=err)
            if value is None:
                if f["required"]:
                    sess.log(f"   ? needs your answer: {label}")
                    return False, key
                continue

        sess.log(f"   Q: {label[:85]}\n   A: {str(value)[:85]}")
        if kind in ("radio", "checkbox"):
            wanted = [v.strip().lower() for v in str(value).split("|")]
            for opt, idx in zip(f["options"], f["idxs"]):
                if opt.strip().lower() in wanted:
                    el = page.locator(f'[data-jd="{idx}"]')
                    try:
                        el.check(force=True, timeout=5000)
                    except Exception:  # noqa: BLE001 - styled inputs: click the label instead
                        page.locator(f'label[for="{el.get_attribute("id")}"]').click()
        elif kind == "select":
            loc.select_option(label=value)
        elif kind == "combobox":
            human.type_text(loc, value)
            human.pause(1.0, 1.8)
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
        else:
            human.type_text(loc, value)
        human.pause(0.4, 1.2)
    return True, None


def errors(page, root: str = "body") -> list[str]:
    """Visible validation errors inside root."""
    return page.evaluate(
        """(root) => { const s = document.querySelector(root) || document.body;
        return [...s.querySelectorAll('[class*="error"], [role="alert"], .artdeco-inline-feedback--error')]
          .filter(e => e.offsetParent && e.innerText.trim()).map(e => e.innerText.trim().slice(0, 120)); }""",
        root)
