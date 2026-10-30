"""Glassdoor: search; "Easy Apply" opens Indeed's smartapply wizard."""
from __future__ import annotations

import re
import time
import urllib.parse

from .. import human
from ..browser import wait_for
from ..config import section
from ..models import make_job
from . import smartapply

PLATFORM = "glassdoor"

CARDS_JS = r"""
() => [...document.querySelectorAll('li[data-test="jobListing"]')].map(li => {
  const q = s => { const e = li.querySelector(s); return e ? e.innerText.trim() : ''; };
  const a = li.querySelector('a[data-test="job-title"]');
  return {id: li.dataset.jobid, title: q('[data-test="job-title"]'), url: a ? a.href : '',
          company: li.innerText.split('\n').map(s => s.trim()).filter(Boolean)[0] || '',
          location: q('[data-test="emp-location"]'), salary: q('[data-test="detailSalary"]'),
          easy: /easy apply/i.test(li.innerText)};
})
"""

STATE_JS = r"""
() => {
  const vis = e => e.getBoundingClientRect().width > 0;
  const btns = [...document.querySelectorAll('button, a')].filter(vis);
  const t = document.body.innerText;
  return {
    easy: !!btns.find(b => /^\s*easy apply\s*$/i.test(b.innerText) || b.dataset.test === 'easyApply'),
    external: !!btns.find(b => /apply on employer site|apply on company site/i.test(b.innerText)),
    applied: /^\s*applied\s*$/im.test(t.slice(0, 6000)) || /you applied/i.test(t.slice(0, 6000)),
    closed: /no longer accepting|job has expired|listing has expired/i.test(t),
    challenge: /verify you are human|just a moment|security check/i.test(t.slice(0, 2000)),
    desc: (document.querySelector('[class*="JobDetails_jobDescription"], [data-test="jobDescriptionContent"], #JobDescriptionContainer') || document.body).innerText.slice(0, 9000),
  };
}
"""


def _base() -> str:
    return f"https://{section('glassdoor').get('domain', 'www.glassdoor.co.in')}"


def _close_popups(page) -> None:
    for sel in ('button[data-test="job-alert-modal-close"]', "button.CloseButton", '[aria-label="Close"]',
                "#onetrust-reject-all-handler"):
        try:
            b = page.locator(sel)
            if b.count() and b.first.is_visible():
                b.first.click(timeout=2000)
        except Exception:  # noqa: BLE001
            pass


def logged_in(page) -> bool:
    page.goto(f"{_base()}/member/home/index.htm", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    return "login" not in page.url.lower()


def discover(ctx, store, log=print) -> list[dict]:
    cfg = section("glassdoor")
    loc_ok = re.compile(cfg.get("location_include") or ".", re.I)
    page = ctx.new_page()
    found = {}
    try:
        for kw in cfg.get("keywords") or []:
            q = urllib.parse.quote(kw)
            try:
                page.goto(f"{_base()}/Job/jobs.htm?sc.keyword={q}&fromAge=3",
                          wait_until="domcontentloaded", timeout=60000)
            except Exception:  # noqa: BLE001
                continue
            time.sleep(5)
            _close_popups(page)
            cards = page.evaluate(CARDS_JS)
            log(f"[glassdoor] '{kw}': {len(cards)} jobs, {sum(c['easy'] for c in cards)} Easy Apply")
            for c in cards:
                if not c["id"] or not c["url"] or not loc_ok.search(c["location"] or ""):
                    continue
                found[c["id"]] = make_job(
                    PLATFORM, c["id"], title=c["title"], company=c["company"], location=c["location"],
                    salary=c["salary"], url=c["url"], apply_kind="easy" if c["easy"] else "external")
            human.between_pages()
    finally:
        page.close()
    return list(found.values())


def open_job(page, job):
    page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
    time.sleep(2)
    _close_popups(page)
    st = wait_for(lambda: (lambda s: s if any(s[k] for k in ("easy", "external", "applied", "closed", "challenge"))
                           else None)(page.evaluate(STATE_JS)), 15) or page.evaluate(STATE_JS)
    job["description"] = st["desc"]
    if st["challenge"]:
        return "blocked", None
    if st["applied"]:
        return "already", None
    if st["easy"]:
        return "apply", None
    return ("external", None) if st["external"] else ("closed", None)


def apply_job(page, job, _handle, sess):
    ctx = page.context
    before = list(ctx.pages)
    btn = page.locator('button[data-test="easyApply"]')
    if not btn.count():
        btn = page.locator("button, a").filter(has_text=re.compile(r"^\s*easy apply\s*$", re.I))
    btn.first.click()
    form = smartapply.find_apply_page(ctx, before, page)
    if not form:
        return "failed", "Easy Apply form did not open"
    try:
        return smartapply.run(form, job, sess)
    finally:
        if form is not page:
            form.close()
