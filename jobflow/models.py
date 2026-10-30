"""The one shape every job has, wherever it was found.

apply_kind says how an application is made, which decides how much the bot may
do on its own:
  easy      - an in-site quick apply (LinkedIn Easy Apply, Naukri, Indeed Easily
              apply, Glassdoor Easy Apply, Unstop). Short, predictable forms.
  ats_form  - the company's own application form (Greenhouse, Lever, Ashby).
  external  - somewhere we cannot apply for you; you get a link.
"""
from __future__ import annotations

import hashlib
import re

APPLY_KINDS = ("easy", "ats_form", "external")

_LEGAL = re.compile(
    r"\b(private|pvt|limited|ltd|llp|llc|inc|incorporated|corp|corporation|co|gmbh|plc|"
    r"india|technologies|technology|tech|solutions|software|services|labs?)\b")
_TITLE_NOISE = re.compile(r"\b(remote|hybrid|onsite|on-site|wfh|work from home|urgent|hiring|immediate joiner)\b")


def board_id(company: str, title: str, url: str) -> str:
    """Stable id for a posting that has no native id of its own."""
    return hashlib.sha1(f"{company}|{title}|{url}".encode()).hexdigest()[:16]


def _norm(s: str, noise: re.Pattern) -> str:
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", (s or "").lower())
    s = re.sub(r"[^a-z0-9+#]+", " ", s)
    s = noise.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def fingerprint(company: str, title: str) -> str:
    """The same role posted on LinkedIn, Naukri and the company site should
    collapse to one key, so it is applied to once. Company legal suffixes and
    title decorations ("(Remote)", "- Urgent") differ between sites; the core
    words do not."""
    return f"{_norm(company, _LEGAL)}|{_norm(title, _TITLE_NOISE)}"


def make_job(platform: str, native_id: str | None, *, title: str, company: str, url: str,
             apply_kind: str, location: str = "", salary: str = "", experience: str = "",
             apply_url: str = "", description: str = "", posted_at: float | None = None,
             domain: str = "", source: str = "") -> dict:
    title = (title or "").strip()
    company = (company or "").strip()
    assert apply_kind in APPLY_KINDS, apply_kind
    jid = f"{platform}:{native_id}" if native_id else f"{platform}:{board_id(company, title, url)}"
    return {
        "id": jid,
        "platform": platform,
        "source": source or platform,
        "apply_kind": apply_kind,
        "title": title,
        "company": company,
        "location": (location or "").strip(),
        "salary": (salary or "").strip(),
        "experience": (experience or "").strip(),
        "url": url or "",
        "apply_url": apply_url or url or "",
        "description": (description or "")[:9000],
        "posted_at": posted_at,
        "domain": domain or "",
        "fingerprint": fingerprint(company, title),
    }
