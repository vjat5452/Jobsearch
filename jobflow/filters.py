"""Cheap checks that run before any model call: title, stack, location,
experience, salary, scam phrases. Each returns a skip reason or None."""
from __future__ import annotations

import re

from .config import PROFILE, section

_F = section("filters")


def _rx(key: str):
    pat = (_F.get(key) or "").strip()
    return re.compile(pat, re.I) if pat else None


TITLE_INCLUDE = _rx("title_include")
TITLE_EXCLUDE = _rx("title_exclude")
STACK_EXCLUDE = _rx("stack_exclude")
RED_FLAGS = [f.lower() for f in _F.get("red_flags") or []]


def red_flag(text: str | None) -> str | None:
    t = (text or "").lower()
    for flag in RED_FLAGS:
        if flag in t:
            return f"scam sign: '{flag}'"
    return None


def min_experience(exp_text: str | None) -> int | None:
    """'2-5 Yrs' -> 2, '3+ years' -> 3."""
    s = exp_text or ""
    m = re.search(r"(\d+)\s*(?:-|to)\s*\d+\s*y", s, re.I) or re.search(r"(\d+)\+?\s*y", s, re.I)
    return int(m.group(1)) if m else None


def salary_max_lpa(sal_text: str | None) -> float | None:
    s = (sal_text or "").lower().replace(",", "")
    m = re.search(r"([\d.]+)\s*-\s*([\d.]+)\s*(?:lac|lpa|lakh)", s)
    if m:
        return float(m.group(2))
    m = re.search(r"([\d.]+)\s*(?:lac|lpa|lakh)", s)
    return float(m.group(1)) if m else None


def prefilter(job: dict) -> str | None:
    title = job.get("title") or ""
    if TITLE_EXCLUDE and TITLE_EXCLUDE.search(title):
        return "title excluded"
    if TITLE_INCLUDE and not TITLE_INCLUDE.search(title):
        return "not a target role"
    if STACK_EXCLUDE and STACK_EXCLUDE.search(title):
        return "different tech stack"
    if (job.get("company") or "").lower() in [c.lower() for c in _F.get("skip_companies") or []]:
        return "company skipped"
    locs = [s.lower() for s in _F.get("locations") or []]
    if locs and job.get("location") and not any(s in job["location"].lower() for s in locs):
        return "location not wanted"
    exp = min_experience(job.get("experience"))
    if exp is not None and exp > int(_F.get("max_required_experience", 99)):
        return f"needs {exp}+ yrs"
    floor = (PROFILE.get("job_preferences") or {}).get("min_ctc_india_lpa")
    top = salary_max_lpa(job.get("salary"))
    if floor and top is not None and top < float(floor):
        return f"salary max {top} LPA below floor"
    return red_flag(" ".join(str(job.get(k) or "") for k in ("title", "company", "description", "salary")))
