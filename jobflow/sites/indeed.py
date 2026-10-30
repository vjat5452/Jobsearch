"""Indeed: search (all jobs, so external ones reach your review queue too);
applies to "Easily apply" ones through smartapply."""
from __future__ import annotations

import re
import time
import urllib.parse

from .. import human
from ..browser import wait_for
from ..config import section
from ..models import make_job
from . import smartapply

PLATFORM = "indeed"

CARDS_JS = r"""
() => [...document.querySelectorAll('a[data-jk]')].map(a => {
  const card = a.closest('.job_seen_beacon, .cardOutline, li') || a.parentElement;
  const q = s => { const e = card.querySelector(s); return e ? e.innerText.trim() : ''; };
  return {jk: a.dataset.jk, title: a.innerText.trim(),
          company: q('[data-testid="company-name"]'), location: q('[data-testid="text-location"]'),
          salary: q('[data-testid*="salary"], .salary-snippet-container, [class*="salary"]'),
          snippet: q('[data-testid="jobsnippet_footer"], .job-snippet, [class*="snippet"]'),
          easy: /easily apply|easy apply/i.test(card.innerText) || !!card.querySelector('[data-testid*="indeedApply"], .iaIcon, .ialbl')};
})
"""

STATE_JS = r"""
() => {
  const vis = e => e.getBoundingClientRect().width > 0;
  const btns = [...document.querySelectorAll('button, a')].filter(vis);
  const t = document.body.innerText;
  return {
    apply: !!(document.querySelector('#indeedApplyButton') || btns.find(b => /^\s*apply now\s*$/i.test(b.innerText))),
    external: !!btns.find(b => /apply on company (site|website)/i.test(b.innerText + ' ' + (b.getAttribute('aria-label') || ''))),
    applied: /you applied|applied on \d|application submitted|^\s*applied\s*$/im.test(t.slice(0, 5000)),
    closed: /this job has expired|no longer available|job has been removed/i.test(t),
    challenge: /verify you are human|additional verification required|just a moment/i.test(t.slice(0, 2000)),
    desc: (document.querySelector('#jobDescriptionText') || document.body).innerText.slice(0, 9000),
  };
}
"""


def _base() -> str:
    return f"https://{section('indeed').get('domain', 'in.indeed.com')}"


def _reject_cookies(page) -> None:
    try:
        b = page.locator("#onetrust-reject-all-handler")
        if b.count() and b.first.is_visible():
            b.first.click()
    except Exception:  # noqa: BLE001
        pass


def logged_in(page) -> bool:
    page.goto(f"{_base()}/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    _reject_cookies(page)
    return not page.locator('a[href*="/account/login"], a[href*="secure.indeed.com/auth"]') \
        .filter(has_text=re.compile("sign in", re.I)).count()


def discover(ctx, store, log=print) -> list[dict]:
    cfg = section("indeed")
    base = _base()
    page = ctx.new_page()
    found = {}
    try:
        for kw in cfg.get("keywords") or []:
            for loc in cfg.get("locations") or [""]:
                q = urllib.parse.urlencode({"q": kw, "l": loc, "fromage": cfg.get("days", 3)})
                try:
                    page.goto(f"{base}/jobs?{q}", wait_until="domcontentloaded", timeout=60000)
                except Exception:  # noqa: BLE001
                    continue
                time.sleep(4)
                _reject_cookies(page)
                if smartapply.CHALLENGE.search(page.inner_text("body")[:2000]):
                    log("[indeed] human-verification page - stopping search for now")
                    return list(found.values())
                cards = page.evaluate(CARDS_JS)
                log(f"[indeed] '{kw}' in {loc or 'anywhere'}: {len(cards)} jobs, "
                    f"{sum(c['easy'] for c in cards)} easily apply")
                for c in cards:
                    found[c["jk"]] = make_job(
                        PLATFORM, c["jk"], title=c["title"], company=c["company"], location=c["location"],
                        salary=c["salary"], description=c["snippet"], url=f"{base}/viewjob?jk={c['jk']}",
                        apply_kind="easy" if c["easy"] else "external")
                human.between_pages()
    finally:
        page.close()
    return list(found.values())


def open_job(page, job):
    page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
    _reject_cookies(page)
    st = wait_for(lambda: (lambda s: s if any(s[k] for k in ("apply", "external", "applied", "closed", "challenge"))
                           else None)(page.evaluate(STATE_JS)), 15) or page.evaluate(STATE_JS)
    job["description"] = st["desc"]
    if st["challenge"]:
        return "blocked", None
    if st["applied"]:
        return "already", None
    if st["apply"]:
        return "apply", None
    return ("external", None) if st["external"] else ("closed", None)


def apply_job(page, job, _handle, sess):
    ctx = page.context
    before = list(ctx.pages)
    btn = page.locator("#indeedApplyButton")
    if not btn.count():
        btn = page.locator("button").filter(has_text=re.compile(r"^\s*apply now\s*$", re.I))
    btn.first.click()
    form = smartapply.find_apply_page(ctx, before, page)
    if not form:
        return "failed", "Indeed apply form did not open"
    try:
        return smartapply.run(form, job, sess)
    finally:
        if form is not page:
            form.close()
