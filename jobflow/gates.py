"""Rules applied in code after the model scores a job.

A prompt is a request, not a guarantee: models comply inconsistently. These two
rules decide whether the bot submits an application in your name, so they are
enforced here, deterministically, and can be re-applied to stored scores for
free (python main.py recap).

seniority - a Senior/Staff/Lead title cannot ride a strong skills match.
relevance - a wrong-field role cannot ride a strong seniority match (an internship
            in an unrelated department scoring 8/10 because it is an internship).
"""
from __future__ import annotations

import re

from .config import PROFILE

CAP = 3

SENIOR = (r"senior", r"sr\.?", r"staff", r"principal", r"lead", r"leads", r"head of", r"director",
          r"vp", r"vice president", r"chief", r"architect", r"manager", r"distinguished", r"fellow",
          r"iii", r"iv")
JUNIOR = (r"intern", r"internship", r"junior", r"jr\.?", r"new ?grad", r"graduate", r"entry[- ]level",
          r"associate", r"apprentice", r"trainee", r"early career", r"university", r"campus", r"fresher")
_SENIOR_RE = re.compile(r"\b(?:" + "|".join(SENIOR) + r")\b", re.I)
_JUNIOR_RE = re.compile(r"\b(?:" + "|".join(JUNIOR) + r")\b", re.I)

SKILLS_FLOOR = 60
DOMAIN_FLOOR = 60


def _candidate_is_senior() -> bool:
    """The seniority cap protects early-career candidates. Someone with 6+ years
    is the audience for senior roles, so the cap switches itself off."""
    try:
        return float((PROFILE.get("experience") or {}).get("total_years") or 0) >= 6
    except (TypeError, ValueError):
        return False


def is_senior(title: str) -> bool:
    """A junior marker wins ('Senior Engineer Intern' is an internship), except
    for director/VP/head titles ('Associate Director' is a director)."""
    t = title or ""
    if _JUNIOR_RE.search(t) and not re.search(r"\bdirector\b|\bvp\b|\bhead of\b", t, re.I):
        return False
    return bool(_SENIOR_RE.search(t))


def seniority_cap(score: int, title: str, reason: str = "") -> tuple[int, str]:
    if score <= CAP or _candidate_is_senior() or not is_senior(title):
        return score, reason
    note = f"capped at {CAP}: senior title"
    return CAP, f"{reason}; {note}" if reason else note


def is_irrelevant(breakdown: dict | None) -> bool:
    if not breakdown:
        return False
    try:
        skills, domain = int(breakdown.get("skills", 0)), int(breakdown.get("domain", 0))
    except (TypeError, ValueError):
        return False
    # All zeros means the model returned nothing usable - missing data, not a bad fit.
    if not any(breakdown.values()):
        return False
    return skills < SKILLS_FLOOR and domain < DOMAIN_FLOOR


def relevance_cap(score: int, breakdown: dict | None, reason: str = "") -> tuple[int, str]:
    if score <= CAP or not is_irrelevant(breakdown):
        return score, reason
    note = f"capped at {CAP}: wrong field"
    return CAP, f"{reason}; {note}" if reason else note


def apply_all(score: int, title: str, breakdown: dict | None, reason: str = "") -> tuple[int, str]:
    score, reason = seniority_cap(score, title, reason)
    return relevance_cap(score, breakdown, reason)
