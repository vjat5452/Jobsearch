"""Company application forms (Greenhouse, Lever, Ashby, Workable).

Discovery for these is plain HTTP and lives in pipeline.discover_boards. These
jobs are routed to review by default, so a job only reaches apply_job after you
approved it in the dashboard.

Company forms often put a CAPTCHA on submit. The bot does not solve them: it
brings its window on screen, notifies you, and waits a few minutes for you.
"""
from __future__ import annotations

import re
import time

from .. import forms, human, notify
from ..browser import place, screenshot, wait_for

PLATFORM = "ats"
SUCCESS = re.compile(r"thank you for applying|application (has been )?(received|submitted)"
                     r"|we('ve| have) received your application|thanks for applying", re.I)
CLOSED = re.compile(r"job (is )?no longer (open|available)|position (has been )?filled|page not found", re.I)
CAPTCHA = 'iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha"], iframe[title*="challenge"]'
FORM_ROOT = "form#application-form, form#application_form, form[action*='apply'], .application-form, form"


def open_job(page, job):
    page.goto(job["apply_url"] or job["url"], wait_until="domcontentloaded", timeout=60000)
    human.pause(2, 4)
    body = page.inner_text("body")[:4000]
    if CLOSED.search(body):
        return "closed", None
    link = page.get_by_role("link", name=re.compile(r"^\s*apply( for this job| now)?\s*$", re.I))
    if link.count():
        link.first.click()
        human.pause(2, 3)
    if not page.locator("input, textarea").count():
        return "closed", None
    if len(job.get("description") or "") < 300:
        job["description"] = page.inner_text("body")[:9000]
    return "apply", None


def apply_job(page, job, _handle, sess):
    tag = job["id"].replace(":", "_")[-40:]
    root = FORM_ROOT if page.locator(FORM_ROOT).count() else "body"
    ok, key = forms.fill(page, job, sess, root=root)
    if not ok:
        return "needs_input", key
    submit = page.get_by_role("button", name=re.compile(r"submit( application)?|^\s*apply\s*$", re.I))
    if not submit.count():
        shot = screenshot(page, f"ats_nosubmit_{tag}")
        return "failed", f"submit button not found [{shot}]"
    if sess.dry_run:
        sess.screenshot = screenshot(page, f"ats_dryrun_{tag}")
        return "dry_run", "form filled, stopped before Submit"
    human.pause(1, 2)
    submit.last.click()
    human.pause(4, 6)

    if page.locator(CAPTCHA).count() and not SUCCESS.search(page.inner_text("body")):
        place(page.context, show=True)
        notify.toast("JobFlow needs you", f"Solve the check to finish: {job['title']} at {job['company']}")
        sess.log("   CAPTCHA shown - window brought on screen, waiting up to 4 min for you")
        done = wait_for(lambda: SUCCESS.search(page.inner_text("body")), 240, every=3)
        place(page.context, show=False)
        if not done:
            shot = screenshot(page, f"ats_captcha_{tag}")
            return "failed", f"CAPTCHA not completed - apply by hand [{shot}]"

    if wait_for(lambda: SUCCESS.search(page.inner_text("body")), 15):
        sess.screenshot = screenshot(page, f"ats_sent_{tag}")
        return "applied", None
    errs = forms.errors(page, root)
    shot = screenshot(page, f"ats_unconfirmed_{tag}")
    return "failed", f"no confirmation: {'; '.join(errs)[:100]} [{shot}]"
