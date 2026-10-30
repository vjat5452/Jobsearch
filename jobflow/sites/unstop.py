"""Unstop: discovery through its public search API (no browser needed); applying
through the job page."""
from __future__ import annotations

import re
import time
import urllib.parse

import requests

from .. import forms, human
from ..browser import screenshot, wait_for
from ..config import section
from ..models import make_job

PLATFORM = "unstop"
API = ("https://unstop.com/api/public/opportunity/search-result?opportunity=jobs&per_page=30"
       "&oppstatus=open&page=1&searchTerm=")
DONE = re.compile(r"successfully (applied|registered)|application (submitted|received)"
                  r"|you have (already )?(applied|registered)|registration (successful|complete)", re.I)
APPLY_BTN = re.compile(r"^\s*(apply|apply now|register|register now|quick apply)\s*$", re.I)

STATE_JS = r"""
() => {
  const vis = e => e.getBoundingClientRect().width > 0;
  const btns = [...document.querySelectorAll('button, a')].filter(vis);
  const t = document.body.innerText;
  return {
    apply: !!btns.find(b => /^\s*(apply|apply now|register|register now|quick apply)\s*$/i.test(b.innerText)),
    applied: /^\s*(applied|registered)\s*$/im.test(t.slice(0, 8000)) || /you have (already )?(applied|registered)/i.test(t),
    closed: /registrations? (closed|ended)|applications? closed|deadline (has )?passed/i.test(t.slice(0, 8000)),
    external: !!btns.find(b => /apply on company|company website/i.test(b.innerText)),
    desc: (document.querySelector('[class*="about"], [class*="description"], main') || document.body).innerText.slice(0, 9000),
  };
}
"""


def _salary(jd: dict) -> str:
    lo, hi = jd.get("min_salary"), jd.get("max_salary")
    if not (lo or hi) or jd.get("not_disclosed"):
        return ""
    mult = 12 if jd.get("pay_in") == "monthly" else 1

    def lpa(v):
        return round(v * mult / 100000, 1) if v else None
    return f"{lpa(lo) or '?'}-{lpa(hi) or '?'} Lacs PA"


def discover(ctx, store, log=print) -> list[dict]:
    cfg = section("unstop")
    loc_ok = re.compile(cfg.get("location_include") or ".", re.I)
    found = {}
    for kw in cfg.get("keywords") or []:
        try:
            r = requests.get(API + urllib.parse.quote(kw), timeout=20,
                             headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            r.raise_for_status()
            items = r.json()["data"]["data"]
        except Exception as e:  # noqa: BLE001
            log(f"[unstop] '{kw}': search failed ({type(e).__name__})")
            continue
        for it in items:
            jd = it.get("jobDetail") or {}
            if not it.get("regn_open") or it.get("isPaid"):
                continue
            cities = [loc.get("city") or "" for loc in it.get("locations") or []]
            location = ", ".join(c for c in cities if c) or (
                "Remote" if jd.get("type") in ("wfh", "remote") or it.get("region") == "online" else "")
            if not loc_ok.search(location or "india"):
                continue
            exp = f"{jd.get('min_experience') or 0}-{jd.get('max_experience')} Yrs" if jd.get("max_experience") else ""
            skills = ", ".join(s.get("skill_name") or s.get("skill") or "" for s in it.get("required_skills") or [])
            found[it["id"]] = make_job(
                PLATFORM, str(it["id"]), title=it.get("title") or "",
                company=(it.get("organisation") or {}).get("name", ""), location=location,
                salary=_salary(jd), experience=exp, apply_kind="easy",
                url=it.get("seo_url") or f"https://unstop.com/{it.get('public_url')}",
                description=f"Skills: {skills}. Work type: {jd.get('type')}, {jd.get('timing')}.")
        log(f"[unstop] '{kw}': {len(items)} open jobs")
    return list(found.values())


def logged_in(page) -> bool:
    page.goto("https://unstop.com/user/dashboard", wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)
    return "login" not in page.url.lower() and "auth" not in page.url.lower()


def open_job(page, job):
    page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
    st = wait_for(lambda: (lambda s: s if any(s[k] for k in ("apply", "applied", "closed", "external"))
                           else None)(page.evaluate(STATE_JS)), 15) or page.evaluate(STATE_JS)
    job["description"] = ((job.get("description") or "") + "\n" + st["desc"])[:9000]
    if st["applied"]:
        return "already", None
    if st["closed"]:
        return "closed", None
    if st["apply"]:
        return "apply", None
    return ("external", None) if st["external"] else ("closed", None)


def apply_job(page, job, _handle, sess):
    tag = job["id"].split(":")[-1]
    page.locator("button, a").filter(has_text=APPLY_BTN).first.click()
    time.sleep(3)
    root = "[role='dialog'], .cdk-overlay-pane, mat-dialog-container, app-registration-form, form"
    for _ in range(8):
        if DONE.search(page.inner_text("body")[:8000]):
            sess.screenshot = screenshot(page, f"unstop_sent_{tag}")
            return "applied", None
        ok, key = forms.fill(page, job, sess, root=root if page.locator(root).count() else "body")
        if not ok:
            return "needs_input", key
        nxt = page.locator("button").filter(
            has_text=re.compile(r"^\s*(submit|next|continue|save and next|register|apply|confirm)\s*$", re.I))
        if not nxt.count():
            break
        if sess.dry_run and re.search(r"submit|register|apply|confirm", nxt.last.inner_text(), re.I):
            sess.screenshot = screenshot(page, f"unstop_dryrun_{tag}")
            return "dry_run", "filled, stopped before the final button"
        human.pause(0.8, 1.8)
        nxt.last.click()
        time.sleep(3)
    if wait_for(lambda: DONE.search(page.inner_text("body")[:8000]), 10):
        sess.screenshot = screenshot(page, f"unstop_sent_{tag}")
        return "applied", None
    page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
    if wait_for(lambda: page.evaluate(STATE_JS)["applied"], 12):
        return "applied", None
    shot = screenshot(page, f"unstop_unconfirmed_{tag}")
    return "failed", f"could not confirm the application [{shot}]"
