"""Naukri.com: search -> job page -> Apply -> answer the recruiter chatbot.

Every in-site Naukri apply is `easy`; jobs that only offer "Apply on company
site" are `external` and go to your review queue."""
from __future__ import annotations

import re
import time

from .. import answers, human
from ..browser import screenshot
from ..config import section
from ..models import make_job

BASE = "https://www.naukri.com"
PLATFORM = "naukri"

# Naukri uses hashed class names (styles_xxx__abc12), so match stable fragments.
CARD_JS = r"""
() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="job-listings-"]')) {
    const m = a.href.match(/-(\d{9,})(?:[?#]|$)/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const card = a.closest('[class*="jobTuple"], [class*="jobtuple"], [class*="job-tuple"], article') || a.parentElement.parentElement;
    const q = s => { const el = card.querySelector(s); return el ? el.innerText.trim() : ''; };
    out.push({
      job_id: m[1], url: a.href.split('?')[0], title: (a.innerText || a.title || '').trim(),
      company: q('[class*="comp-name"], [class*="companyInfo"] a, [class*="comp-dtls"] a'),
      experience: q('[class*="exp-wrap"], [class*="expwdth"], [class*="experience"]'),
      salary: q('[class*="sal-wrap"], [class*="salary"]'),
      location: q('[class*="loc-wrap"], [class*="locWdth"], [class*="location"]'),
      tags: [...card.querySelectorAll('[class*="tags"] li, [class*="tag-li"]')].map(e => e.innerText.trim()).join(', '),
    });
  }
  return out;
}
"""

JD_JS = r"""
() => {
  const el = document.querySelector('[class*="dang-inner-html"], [class*="job-desc"], [class*="JDC"], section[class*="jd"]');
  const head = document.querySelector('[class*="jd-header"], [class*="jdHeader"]');
  return ((head ? head.innerText + '\n\n' : '') + (el ? el.innerText : document.body.innerText)).slice(0, 9000);
}
"""

DRAWER = ('[class*="chatbot_Drawer"], [class*="chatbot_drawer"], [class*="chatbot_Container"], '
          '[class*="chatbot_MessageContainer"]')

CHAT_STATE_JS = r"""
(drawer) => {
  const txt = el => (el.innerText || '').trim();
  const bots = [...drawer.querySelectorAll('[class*="botMsg"], [class*="bot-msg"], [class*="botItem"]')];
  const labelFor = inp => {
    const l = inp.id && drawer.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
    return txt(l || inp.closest('label') || inp.parentElement) || inp.value;
  };
  return {
    question: bots.length ? txt(bots[bots.length - 1]) : '',
    radios: [...drawer.querySelectorAll('input[type="radio"]')].map(labelFor),
    checks: [...drawer.querySelectorAll('input[type="checkbox"]')].map(labelFor),
    chips: [...drawer.querySelectorAll('[class*="chatbot_Chip"], [class*="chipItem"], [class*="suggestedChip"]')].map(txt).filter(Boolean),
    text: !!drawer.querySelector('[contenteditable="true"], textarea, input[type="text"], input[type="number"]'),
  };
}
"""

CLICK_OPTION_JS = r"""
(drawer, {kind, wanted}) => {
  const txt = el => (el && el.innerText || '').trim();
  const labelFor = inp => {
    const l = inp.id && drawer.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
    return l || inp.closest('label') || inp.parentElement;
  };
  let n = 0;
  if (kind === 'chips') {
    for (const c of drawer.querySelectorAll('[class*="chatbot_Chip"], [class*="chipItem"], [class*="suggestedChip"]'))
      if (wanted.includes(txt(c))) { c.click(); n++; break; }
  } else {
    for (const inp of drawer.querySelectorAll(`input[type="${kind}"]`)) {
      const lab = labelFor(inp);
      if (wanted.includes(txt(lab) || inp.value)) { (lab || inp).click(); if (!inp.checked) inp.click(); n++; }
    }
  }
  return n;
}
"""

SUCCESS = re.compile(r"successfully applied|applied successfully|you have applied|application (has been )?sent", re.I)
CHAT_DONE = re.compile(r"thank you for your (responses?|answers?)|thanks for your (responses?|answers?)"
                       r"|application (is )?(submitted|complete)", re.I)
QUOTA = re.compile(r"daily (apply )?(quota|limit)|reached the (maximum|max) (number|limit)", re.I)
CHALLENGE = re.compile(r"access denied|verify you are human|unusual activity", re.I)


def _cfg() -> dict:
    return section("naukri")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def search_url(keyword: str, location: str, page_no: int) -> str:
    cfg = _cfg()
    url = f"{BASE}/{_slug(keyword)}-jobs-in-{_slug(location)}"
    if page_no > 1:
        url += f"-{page_no}"
    return f"{url}?experience={cfg.get('experience_years', 1)}&jobAge={cfg.get('job_age_days', 3)}"


def logged_in(page) -> bool:
    page.goto(f"{BASE}/mnjuser/homepage", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    return "login" not in page.url.lower()


def discover(ctx, store, log=print) -> list[dict]:
    cfg = _cfg()
    page = ctx.new_page()
    found = {}
    try:
        for kw in cfg.get("keywords") or []:
            for loc in cfg.get("locations") or ["india"]:
                for p in range(1, int(cfg.get("pages_per_search", 1)) + 1):
                    page.goto(search_url(kw, loc, p), wait_until="domcontentloaded", timeout=60000)
                    human.pause(2, 4)
                    human.scroll(page)
                    cards = page.evaluate(CARD_JS)
                    log(f"[naukri] '{kw}' in {loc} p{p}: {len(cards)} jobs")
                    for c in cards:
                        found[c["job_id"]] = make_job(
                            PLATFORM, c["job_id"], title=c["title"], company=c["company"],
                            location=c["location"], salary=c["salary"], experience=c["experience"],
                            url=c["url"], apply_kind="easy", description=c["tags"])
                    human.between_pages()
                    if not cards:
                        break
    finally:
        page.close()
    return list(found.values())


def _button(page, css: str, pattern: str):
    """Naukri's stable button ids first, then any button whose text matches."""
    loc = page.locator(css)
    if loc.count():
        return loc.first
    loc = page.locator("button").filter(has_text=re.compile(pattern, re.I))
    return loc.first if loc.count() else None


def _page_state(page):
    if _button(page, "#already-applied", r"^\s*applied\s*$"):
        return "already", None
    if _button(page, "#company-site-button", r"company site"):
        return "external", None
    btn = _button(page, "#apply-button", r"^\s*(apply|i am interested)\s*$")
    if btn and re.match(r"^\s*applied\s*$", btn.inner_text(), re.I):
        return "already", None
    return ("apply", btn) if btn else (None, None)


def _body(page) -> str:
    try:
        return page.inner_text("body")[:20000]
    except Exception:  # noqa: BLE001
        return ""


def open_job(page, job):
    page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
    human.pause(2, 4)
    if CHALLENGE.search(_body(page)[:3000]):
        return "blocked", None
    human.scroll(page, 2)
    job["description"] = page.evaluate(JD_JS)
    state, btn = _page_state(page)
    return (state or "closed"), btn


def _confirm_applied(page, job, sess):
    """Naukri often redirects to 'similar jobs' after applying, so re-open the
    job page and look for 'Applied'."""
    tag = job["id"].split(":")[-1]
    for _ in range(3):
        time.sleep(2)
        if SUCCESS.search(_body(page)) or _page_state(page)[0] == "already":
            sess.screenshot = screenshot(page, f"naukri_applied_{tag}")
            return "applied", None
    for _ in range(2):
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=60000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(4)
        if _page_state(page)[0] == "already":
            sess.screenshot = screenshot(page, f"naukri_applied_{tag}")
            return "applied", None
    shot = screenshot(page, f"naukri_unconfirmed_{tag}")
    return "failed", f"questions answered but job not marked Applied [{shot}]"


def _answer_chat(page, job, sess):
    tag = job["id"].split(":")[-1]
    last_q, repeats, idle = None, 0, 0
    for _ in range(25):
        body = _body(page)
        if SUCCESS.search(body):
            sess.screenshot = screenshot(page, f"naukri_applied_{tag}")
            return "applied", None
        if QUOTA.search(body):
            return "quota", "naukri daily limit reached"

        drawer = page.locator(DRAWER).first
        if not drawer.count() or not drawer.is_visible():
            idle += 1
            if idle >= 4:
                return _confirm_applied(page, job, sess)
            time.sleep(1.5)
            continue
        idle = 0

        st = drawer.evaluate(CHAT_STATE_JS)
        q = st["question"]
        if not q:
            time.sleep(1.5)
            continue
        if CHAT_DONE.search(q):
            return _confirm_applied(page, job, sess)
        if not (st["radios"] or st["checks"] or st["chips"] or st["text"]):
            idle += 1                       # a bot message with nothing to answer
            if idle >= 4:
                return _confirm_applied(page, job, sess)
            time.sleep(1.5)
            continue
        if q == last_q:
            repeats += 1
            if repeats >= 2:
                shot = screenshot(page, f"naukri_stuck_{tag}")
                return "failed", f"stuck on: {q[:80]} [{shot}]"
        else:
            last_q, repeats = q, 0

        if st["radios"]:
            kind, options = "radio", st["radios"]
        elif st["checks"]:
            kind, options = "checkbox", st["checks"]
        elif st["chips"]:
            kind, options = "chips", st["chips"]
        else:
            kind, options = "text", []

        ans, key = answers.resolve(sess, q, kind, options, job)
        if ans is None:
            sess.log(f"   ? needs your answer: {q}")
            return "needs_input", key
        sess.log(f"   Q: {q[:90]}\n   A: {ans[:90]}")
        human.pause()

        if kind in ("radio", "checkbox", "chips"):
            # click the option control itself - never chat text (old answers like "Yes" are in the chat too)
            wanted = [a.strip() for a in ans.split("|")] if kind == "checkbox" else [ans]
            if not drawer.evaluate(CLICK_OPTION_JS, {"kind": kind, "wanted": wanted}):
                shot = screenshot(page, f"naukri_option_{tag}")
                return "failed", f"could not click option '{ans}' [{shot}]"
        else:
            box = drawer.locator('[contenteditable="true"], textarea, input[type="text"], input[type="number"]').first
            human.type_text(box, ans)

        send = drawer.locator('[class*="sendMsg"], [class*="send-btn"], [class*="sendBtn"]').first
        if send.count() and send.is_visible():
            send.click()
        else:
            save = drawer.get_by_role("button", name=re.compile(r"save|submit|send|next", re.I))
            if save.count():
                save.first.click()
        human.pause(1.5, 3)
    return "failed", "too many chatbot steps"


def apply_job(page, job, btn, sess):
    if sess.dry_run:
        # Naukri submits the moment Apply is pressed (the chatbot only follows
        # when the recruiter set questions), so a dry run stops here.
        sess.screenshot = screenshot(page, f"naukri_dryrun_{job['id'].split(':')[-1]}")
        return "dry_run", "Apply button found; Naukri submits on click, so not pressed"
    try:
        btn.click()
        human.pause(2, 4)
        return _answer_chat(page, job, sess)
    except Exception as e:  # noqa: BLE001
        # the error may have come after Naukri already accepted the application
        status, note = _confirm_applied(page, job, sess)
        if status == "failed":
            note = f"error: {str(e).splitlines()[0][:80]}"
        return status, note
