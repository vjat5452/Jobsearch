"""The hybrid: decide, per scored job, whether the bot applies on its own, waits
for your approval, or leaves it.

  auto   in-site quick apply (apply_kind 'easy') scoring >= auto_min, low scam risk
  review everything else scoring >= review_min - company forms, external links,
         easy-apply jobs in the grey zone, medium scam risk
  skip   below review_min, high scam risk, scam phrase, or a duplicate

A role posted on several sites is applied to once. Of the copies, the one on the
site earliest in routing.prefer wins; a copy you already approved or that was
already applied to always wins.
"""
from __future__ import annotations

from . import filters
from .config import section

LOCKED = ("approved", "applying", "applied", "manual", "needs_input")
SOFT = ("queued", "review")


def _cfg() -> dict:
    return section("routing")


def thresholds(platform: str) -> tuple[bool, int, int]:
    cfg = _cfg()
    over = (cfg.get("platforms") or {}).get(platform) or {}
    return (bool(over.get("auto", True)),
            int(over.get("auto_min", cfg.get("auto_min", 7))),
            int(over.get("review_min", cfg.get("review_min", 6))))


def _rank(platform: str) -> int:
    prefer = _cfg().get("prefer") or []
    return prefer.index(platform) if platform in prefer else len(prefer)


def decide(job: dict) -> tuple[str, str, str]:
    """(route, status, note) for one scored job, ignoring duplicates."""
    score = job.get("score")
    if score is None:
        return "", "new", ""
    if job.get("scam_risk") == "high":
        return "skip", "skipped", "high scam risk"
    flag = filters.red_flag(job.get("description"))
    if flag:
        return "skip", "skipped", flag
    auto_ok, auto_min, review_min = thresholds(job["platform"])
    if score < review_min:
        return "skip", "skipped", f"low match ({score})"
    if job["apply_kind"] == "easy" and auto_ok and score >= auto_min and job.get("scam_risk") != "medium":
        return "auto", "queued", ""
    if job["apply_kind"] == "external":
        why = "apply on the company site yourself"
    elif job["apply_kind"] == "ats_form":
        why = "company application form"
    elif job.get("scam_risk") == "medium":
        why = "medium scam risk - check it"
    elif not auto_ok:
        why = f"auto-apply off for {job['platform']}"
    else:
        why = f"below auto threshold ({score} < {auto_min})"
    return "review", "review", why


def route_job(store, job: dict) -> str:
    route, status, note = decide(job)
    if not route:
        return ""
    if route != "skip" and job.get("fingerprint"):
        for sib in store.siblings(job["fingerprint"], job["id"]):
            if sib["closed_at"] is not None and sib["status"] not in ("applied", "manual"):
                continue
            if sib["status"] in LOCKED or (
                    sib["status"] in SOFT and _rank(sib["platform"]) <= _rank(job["platform"])):
                store.update(job["id"], route="skip", status="duplicate",
                             note=f"same role on {sib['platform']} ({sib['status']})")
                return "duplicate"
            if sib["status"] in SOFT:
                # this copy is on a preferred site: it takes over
                store.update(sib["id"], route="skip", status="duplicate",
                             note=f"same role on {job['platform']}")
    store.update(job["id"], route=route, status=status, note=note or None)
    return route


def route_pending(store) -> dict:
    counts = {"auto": 0, "review": 0, "skip": 0, "duplicate": 0}
    # Best score first, and among equals the preferred site first, so the copy
    # that should win a duplicate is routed before the ones it beats.
    for job in sorted(store.unrouted(), key=lambda j: (-(j["score"] or 0), _rank(j["platform"]))):
        r = route_job(store, job)
        if r:
            counts[r] += 1
    return counts
