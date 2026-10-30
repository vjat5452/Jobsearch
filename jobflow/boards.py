"""Company career boards and free aggregators, over plain HTTP. No browser, no login.

Every adapter returns jobs in the shape from models.make_job. fetch_company never
raises: one dead board must not kill the run, and a board that failed must not
have its jobs marked closed (see Store.mark_closed).
"""
from __future__ import annotations

import html
import re
import time
from typing import Any

import requests

from .models import make_job

UA = {"User-Agent": "jobflow/0.1 (personal job tracker)"}
TIMEOUT = 25
PLATFORM = "ats"          # every company board is the "ats" platform in the queue
AGGREGATOR = "remote"


def _get(url: str) -> Any:
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post(url: str, body: dict) -> Any:
    r = requests.post(url, headers={**UA, "Content-Type": "application/json",
                                    "Accept": "application/json"}, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _epoch(value) -> float | None:
    """Boards return ms ints, ISO strings, or nothing. Normalise to epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _text(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(h or ""))).strip()


# ---------------------------------------------------------------------------
# Boards we can also apply on (their forms are public): ats_form
# ---------------------------------------------------------------------------

def greenhouse(name: str, slug: str) -> list[dict]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [make_job(
        PLATFORM, f"gh:{slug}:{j['id']}", title=j.get("title"), company=name,
        url=j.get("absolute_url"), apply_kind="ats_form",
        apply_url=f"https://job-boards.greenhouse.io/{slug}/jobs/{j['id']}",
        location=(j.get("location") or {}).get("name", ""),
        # Greenhouse gives `updated_at`, so "posted" can mean "edited".
        posted_at=_epoch(j.get("updated_at") or j.get("first_published")),
        description=_text(j.get("content", "")), source="greenhouse")
        for j in data.get("jobs", [])]


def lever(name: str, slug: str) -> list[dict]:
    out = []
    for j in _get(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        cats = j.get("categories") or {}
        loc = cats.get("location", "") + (f" / {j['workplaceType']}" if j.get("workplaceType") else "")
        out.append(make_job(
            PLATFORM, f"lever:{slug}:{j['id']}", title=j.get("text"), company=name,
            url=j.get("hostedUrl"), apply_kind="ats_form",
            apply_url=j.get("applyUrl") or f"{j.get('hostedUrl')}/apply", location=loc,
            posted_at=_epoch(j.get("createdAt")), description=j.get("descriptionPlain", ""),
            source="lever"))
    return out


def ashby(name: str, slug: str) -> list[dict]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    return [make_job(
        PLATFORM, f"ashby:{slug}:{j.get('id')}", title=j.get("title"), company=name,
        url=j.get("jobUrl"), apply_kind="ats_form",
        apply_url=j.get("applyUrl") or f"{j.get('jobUrl')}/application",
        location=j.get("location", ""), posted_at=_epoch(j.get("publishedAt")),
        description=j.get("descriptionPlain", ""), source="ashby")
        for j in data.get("jobs", [])]


def workable(name: str, slug: str) -> list[dict]:
    data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    return [make_job(
        PLATFORM, None, title=j.get("title"), company=name,
        url=j.get("url") or j.get("application_url"), apply_kind="ats_form",
        apply_url=j.get("application_url") or j.get("url"), location=j.get("location", ""),
        posted_at=_epoch(j.get("published_on")), description=_text(j.get("description", "")),
        source="workable")
        for j in data.get("jobs", [])]


# ---------------------------------------------------------------------------
# Enterprise boards. Large employers rarely use Greenhouse; most sit on Workday
# or Oracle Recruiting Cloud. Their apply flows need an account per employer, so
# these are `external`: ranked and shown to you, applied to by you.
# ---------------------------------------------------------------------------

def workday(name: str, slug: str) -> list[dict]:
    """slug is 'tenant/wdN/site', e.g. 'adobe/wd5/external_experienced'."""
    tenant, wd, site = slug.split("/")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out: list[dict] = []
    for offset in range(0, 200, 20):        # pages of 20; a few pages is plenty per run
        postings = _post(api, {"appliedFacets": {}, "limit": 20, "offset": offset,
                               "searchText": ""}).get("jobPostings") or []
        for j in postings:
            out.append(make_job(
                PLATFORM, None, title=j.get("title"), company=name,
                url=f"{base}/{site}{j.get('externalPath') or ''}", apply_kind="external",
                location=j.get("locationsText", ""),
                description=" ".join(j.get("bulletFields") or []), source="workday"))
        if len(postings) < 20:
            break
    return out


def oracle_cloud(name: str, slug: str) -> list[dict]:
    """slug is 'host/siteNumber', e.g. 'eeho.fa.us2.oraclecloud.com/CX_1'."""
    host, site = slug.split("/", 1)
    out: list[dict] = []
    for offset in (0, 200, 400):
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={site},limit=200,offset={offset}")
        reqs = ((_get(url).get("items") or [{}])[0]).get("requisitionList") or []
        for j in reqs:
            out.append(make_job(
                PLATFORM, None, title=j.get("Title"), company=name,
                url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id') or ''}",
                apply_kind="external", location=j.get("PrimaryLocation") or "",
                posted_at=_epoch(j.get("PostedDate")),
                description=j.get("ShortDescriptionStr") or "", source="oracle"))
        if len(reqs) < 200:
            break
    return out


def atlassian_board(name: str, slug: str) -> list[dict]:
    """Atlassian publishes its own JSON feed rather than using a hosted board."""
    out = []
    for j in _get("https://www.atlassian.com/endpoint/careers/listings"):
        portal = j.get("portalJobPost") or {}
        out.append(make_job(
            PLATFORM, None, title=j.get("title"), company=name, url=portal.get("portalUrl"),
            apply_kind="external", location="; ".join((j.get("locations") or [])[:2]),
            posted_at=_epoch(portal.get("updatedDate")), description=_text(j.get("overview")),
            source="atlassian"))
    return out


BOARDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
          "workday": workday, "oracle": oracle_cloud, "atlassian": atlassian_board}


def fetch_company(entry: dict) -> tuple[list[dict], str | None]:
    """Returns (jobs, error). Never raises."""
    fn = BOARDS.get((entry.get("board") or "greenhouse").lower())
    if not fn:
        return [], f"unknown board '{entry.get('board')}'"
    try:
        jobs = fn(entry["name"], entry["slug"])
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {str(e)[:120]}"
    for j in jobs:
        j["domain"] = entry.get("domain", "")
    return jobs, None


# ---------------------------------------------------------------------------
# Aggregators (keyless, discovery only - they link out to wherever you apply)
# ---------------------------------------------------------------------------

def remotive(query: str, limit: int = 30) -> list[dict]:
    data = _get(f"https://remotive.com/api/remote-jobs?search={requests.utils.quote(query)}&limit={limit}")
    return [make_job(
        AGGREGATOR, None, title=j.get("title"), company=j.get("company_name", "?"),
        url=j.get("url"), apply_kind="external",
        location=j.get("candidate_required_location", "Remote"),
        posted_at=_epoch(j.get("publication_date")), description=_text(j.get("description", "")),
        source="remotive")
        for j in data.get("jobs", [])[:limit]]


def arbeitnow(query: str, limit: int = 30) -> list[dict]:
    q, out = query.lower(), []
    for j in _get("https://www.arbeitnow.com/api/job-board-api").get("data", []):
        hay = f"{j.get('title', '')} {' '.join(j.get('tags') or [])}".lower()
        if q in hay:
            out.append(make_job(
                AGGREGATOR, None, title=j.get("title"), company=j.get("company_name", "?"),
                url=j.get("url"), apply_kind="external", location=j.get("location", ""),
                posted_at=_epoch(j.get("created_at")), description=_text(j.get("description", "")),
                source="arbeitnow"))
        if len(out) >= limit:
            break
    return out


def discover_aggregators(queries: list[str], max_per_query: int = 30) -> tuple[list[dict], list[str]]:
    jobs, errors = [], []
    for q in queries:
        for fn in (remotive, arbeitnow):
            try:
                jobs.extend(fn(q, max_per_query))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{fn.__name__}({q}): {type(e).__name__}")
            time.sleep(0.6)          # be polite to free endpoints
    return jobs, errors
