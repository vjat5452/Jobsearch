"""Indeed's application wizard (smartapply.indeed.com), used by Indeed
"Easily apply" and Glassdoor "Easy Apply".
Pages: contact info -> resume -> questions -> (experience) -> review -> submit."""
from __future__ import annotations

import re
import time

from .. import forms, human
from ..browser import screenshot, wait_for

DONE = re.compile(r"application (has been )?submitted|your application has been sent|you.ve applied"
                  r"|applied successfully", re.I)
CHALLENGE = re.compile(r"verify you are human|additional verification required|just a moment"
                       r"|complete the security check", re.I)
CONTINUE = re.compile(r"^\s*(continue|next|review your application|review)\s*$", re.I)
SUBMIT = re.compile(r"^\s*submit (your )?application\s*$", re.I)


def find_apply_page(ctx, before_pages, page, timeout: float = 20):
    """Clicking Apply opens smartapply in the same tab or a new one - return it."""
    def look():
        for p in ctx.pages:
            if "smartapply.indeed.com" in p.url and (p not in before_pages or p is page):
                return p
        return None
    return wait_for(look, timeout)


def run(page, job, sess):
    tag = job["id"].split(":")[-1]
    last, same = None, 0
    for _ in range(14):
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2.5)
        body = page.inner_text("body")[:6000]
        if DONE.search(body):
            sess.screenshot = screenshot(page, f"smartapply_sent_{tag}")
            return "applied", None
        if CHALLENGE.search(body):
            return "blocked", "human verification shown"

        ok, key = forms.fill(page, job, sess, root="main")
        if not ok:
            return "needs_input", key

        submit = page.locator("button").filter(has_text=SUBMIT)
        if submit.count() and sess.dry_run:
            sess.screenshot = screenshot(page, f"smartapply_dryrun_{tag}")
            return "dry_run", "filled every page, stopped before Submit"
        nxt = submit if submit.count() else page.locator("button").filter(has_text=CONTINUE)
        if not nxt.count():
            shot = screenshot(page, f"smartapply_nobutton_{tag}")
            return "failed", f"no Continue/Submit button [{shot}]"
        human.pause(0.8, 1.8)
        nxt.first.click()
        if submit.count() and wait_for(lambda: DONE.search(page.inner_text("body")[:6000]), 20):
            sess.screenshot = screenshot(page, f"smartapply_sent_{tag}")
            return "applied", None

        time.sleep(2)
        step = page.url + "|" + re.sub(r"\s+", " ", page.inner_text("body")[:200])
        if step == last:
            same += 1
            if same >= 2:
                errs = forms.errors(page, "main")
                shot = screenshot(page, f"smartapply_stuck_{tag}")
                return "failed", f"form rejected: {'; '.join(errs)[:100] or 'unknown field'} [{shot}]"
        else:
            last, same = step, 0
    return "failed", "too many steps"
