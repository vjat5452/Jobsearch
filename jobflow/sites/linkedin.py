"""LinkedIn: job search + Easy Apply collection; applies through the Easy Apply
dialog (Next -> Review -> Submit application). Jobs without Easy Apply are kept
as `external` and routed to your review queue with a link."""
from __future__ import annotations

import re
import time
import urllib.parse

from .. import forms, human
from ..browser import screenshot, wait_for
from ..config import section
from ..models import make_job

PLATFORM = "linkedin"
DIALOG = "dialog[open], div[role='dialog'].jobs-easy-apply-modal, .jobs-easy-apply-modal"

CARDS_JS = r"""
() => [...document.querySelectorAll('li[data-occludable-job-id]')].map(li => {
  const q = s => { const e = li.querySelector(s); return e ? e.innerText.trim() : ''; };
  const lines = li.innerText.split('\n').map(s => s.trim()).filter(Boolean);
  const meta = [...li.querySelectorAll('.job-card-container__metadata-wrapper, [class*="metadata"]')].map(e => e.innerText.trim());
  const a = li.querySelector('a[href*="/jobs/view/"]');
  return {
    id: li.dataset.occludableJobId,
    title: (a && a.innerText.split('\n')[0].trim()) || lines[0] || '',
    company: q('.artdeco-entity-lockup__subtitle'),
    location: q('.artdeco-entity-lockup__caption'),
    salary: meta.find(m => /inr|₹|\$|€|£|\/yr|\/hr|lpa/i.test(m)) || '',
    applied: /\bApplied\b/.test(li.innerText),
    easy: /Easy Apply/.test(li.innerText),
  };
})
"""

STATE_JS = r"""
() => {
  const vis = e => e.getBoundingClientRect().width > 0;
  const btns = [...document.querySelectorAll('button, a')].filter(vis);
  const easy = btns.find(b => /^Easy Apply/i.test(b.getAttribute('aria-label') || '') || /^\s*Easy Apply\s*$/.test(b.innerText));
  const ext = btns.find(b => /company website|apply on/i.test(b.getAttribute('aria-label') || '') || /^\s*Apply\s*$/.test(b.innerText));
  const text = (document.querySelector('main') || document.body).innerText;
  return {
    easy: !!easy, external: !!ext,
    applied: /Applied \d+ (second|minute|hour|day|week|month)s? ago|Application submitted|You applied/i.test(text.slice(0, 4000)),
    closed: /No longer accepting applications/i.test(text),
    challenge: /security verification|let's do a quick security check|verify you('re| are) (a )?human/i.test(text.slice(0, 3000)),
    desc: text.slice(0, 9000),
  };
}
"""

DONE = re.compile(r"application (was )?sent|your application was submitted|applied successfully", re.I)
# Must mention Easy Apply/applications: "you have reached the limit of 3 responses"
# inside an education list is not it.
LIMIT = re.compile(r"(you['’]ve|you have) (reached|hit) [^.\n]{0,60}(easy apply|application)[^.\n]{0,30}limit"
                   r"|(easy apply|daily application|application) limit (for|reached)", re.I)
STEP_BUTTON = re.compile(r"^\s*(Next|Review|Continue|Submit application|Done)\s*$", re.I)


def _cfg() -> dict:
    return section("linkedin")


def logged_in(page) -> bool:
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    return not re.search(r"/login|/authwall|/checkpoint|/signup|/uas/", page.url)


def _collect(page, url: str, log, label: str) -> list[dict]:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)
    for _ in range(3):                      # cards render only once scrolled into view
        page.evaluate("() => document.querySelectorAll('li[data-occludable-job-id]')"
                      ".forEach(li => li.scrollIntoView({block: 'center'}))")
        time.sleep(0.8)
    cards = [c for c in page.evaluate(CARDS_JS) if c["id"] and c["title"]]
    log(f"[linkedin] {label}: {len(cards)} jobs")
    out = []
    for c in cards:
        if c["applied"]:
            continue
        out.append(make_job(
            PLATFORM, c["id"], title=c["title"], company=c["company"], location=c["location"],
            salary=c["salary"], url=f"https://www.linkedin.com/jobs/view/{c['id']}/",
            apply_kind="easy" if c["easy"] else "external"))
    return out


def discover(ctx, store, log=print) -> list[dict]:
    cfg = _cfg()
    page = ctx.new_page()
    found: dict[str, dict] = {}
    try:
        days = int(cfg.get("days", 3)) * 86400
        for kw in cfg.get("keywords") or []:
            q = urllib.parse.urlencode({"keywords": kw, "location": cfg.get("location", ""),
                                        "f_TPR": f"r{days}", "f_AL": "true"})   # f_AL = Easy Apply only
            for job in _collect(page, f"https://www.linkedin.com/jobs/search/?{q}", log, f"'{kw}'"):
                found[job["id"]] = job
            human.between_pages()
        for start in range(0, 25 * int(cfg.get("pages", 6)), 25):
            jobs = _collect(page, f"https://www.linkedin.com/jobs/collections/easy-apply/?start={start}",
                            log, f"easy-apply collection p{start // 25 + 1}")
            if not jobs:
                break
            for job in jobs:
                found.setdefault(job["id"], job)
            human.between_pages()
    finally:
        page.close()
    return list(found.values())


def open_job(page, job):
    page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
    st = wait_for(lambda: (lambda s: s if any(s[k] for k in ("easy", "external", "applied", "closed", "challenge"))
                           else None)(page.evaluate(STATE_JS)), 15) or page.evaluate(STATE_JS)
    job["description"] = st["desc"]
    human.pause(1, 2.5)
    if st["challenge"]:
        return "blocked", None
    if st["applied"]:
        return "already", None
    if st["closed"]:
        return "closed", None
    if st["easy"]:
        return "apply", None
    return ("external", None) if st["external"] else ("closed", None)


def _dialog_text(page) -> str:
    try:
        return page.locator(DIALOG).last.inner_text(timeout=3000)
    except Exception:  # noqa: BLE001
        return ""


def _button(dlg, pattern: str):
    loc = dlg.locator("button").filter(has_text=re.compile(pattern, re.I))
    return loc.first if loc.count() else None


def _discard(page) -> None:
    """Close the Easy Apply dialog without submitting."""
    try:
        page.keyboard.press("Escape")
        time.sleep(1.5)
        d = page.get_by_role("button", name="Discard")
        if d.count():
            d.first.click()
        time.sleep(1)
    except Exception:  # noqa: BLE001
        pass


def _ready(page, timeout: float = 20):
    def check():
        if not page.locator(DIALOG).count():
            return None
        if page.locator(DIALOG).last.locator("button").filter(has_text=STEP_BUTTON).count():
            return True
        t = _dialog_text(page)
        return True if (DONE.search(t) or LIMIT.search(t)) else None
    return wait_for(check, timeout)


def apply_job(page, job, _handle, sess):
    tag = job["id"].split(":")[-1]
    easy = page.locator("button").filter(has_text=re.compile(r"^\s*Easy Apply\s*$")).first
    if not easy.count():
        easy = page.get_by_role("button", name=re.compile(r"^Easy Apply", re.I)).first
    easy.click()
    if not _ready(page, 25):
        shot = screenshot(page, f"linkedin_noload_{tag}")
        _discard(page)
        return "failed", f"Easy Apply dialog did not load [{shot}]"

    last_step, same = None, 0
    for _ in range(15):
        _ready(page)
        text = _dialog_text(page)
        m = LIMIT.search(text)
        if m:
            _discard(page)
            return "quota", "LinkedIn: " + text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")
        if DONE.search(text):
            break

        ok, key = forms.fill(page, job, sess, root=DIALOG)
        if not ok:
            _discard(page)
            return "needs_input", key

        dlg = page.locator(DIALOG).last
        submit = _button(dlg, r"^\s*Submit application\s*$")
        if submit and sess.dry_run:
            sess.screenshot = screenshot(page, f"linkedin_dryrun_{tag}")
            _discard(page)
            return "dry_run", "filled every step, stopped before Submit"
        nxt = submit or _button(dlg, r"^\s*(Next|Review|Continue)\s*$")
        if not nxt:
            shot = screenshot(page, f"linkedin_nobutton_{tag}")
            _discard(page)
            return "failed", f"no Next/Submit button [{shot}]"
        human.pause(0.8, 1.8)
        nxt.click()
        time.sleep(2.5)

        if submit:
            if wait_for(lambda: DONE.search(_dialog_text(page)) or not page.locator(DIALOG).count(), 15):
                break
        step = re.sub(r"\s+", " ", _dialog_text(page))[:160]
        if step == last_step:
            same += 1
            if same >= 2:                   # filled what we could; the form will not advance
                errs = forms.errors(page, DIALOG)
                shot = screenshot(page, f"linkedin_stuck_{tag}")
                _discard(page)
                return "failed", f"form rejected: {'; '.join(errs)[:100] or 'unknown field'} [{shot}]"
        else:
            last_step, same = step, 0
    else:
        _discard(page)
        return "failed", "too many steps"

    sess.screenshot = screenshot(page, f"linkedin_sent_{tag}")
    time.sleep(2)
    for name in ("Done", "Dismiss", "Not now"):
        b = page.get_by_role("button", name=name)
        if b.count():
            try:
                b.first.click(timeout=3000)
                break
            except Exception:  # noqa: BLE001
                pass
    # Trust the job page, not the dialog: re-open it and look for "Applied".
    page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
    if wait_for(lambda: page.evaluate(STATE_JS)["applied"], 12):
        return "applied", None
    return "failed", "submitted but LinkedIn does not show Applied"
