"""The stages, usable one at a time from the CLI or chained by the worker:

  discover -> filter -> store (diff) -> score -> route -> apply
"""
from __future__ import annotations

from datetime import datetime

from . import boards, filters, gates, human, llm, router, scoring, sites, tailor
from .browser import screenshot
from .config import section
from .resume import candidate_summary

# Searches never prove a posting is gone; a job not seen again for this long is
# treated as closed (company boards are exact and close on the next clean fetch).
SEARCH_EXPIRY_DAYS = 14


def ingest(store, jobs: list[dict]) -> tuple[int, int]:
    """Store jobs; filter the new ones. Returns (new, kept)."""
    fresh = store.upsert(jobs)
    kept = 0
    for j in fresh:
        reason = filters.prefilter(j)
        if reason:
            store.skip_new(j["id"], reason)
        else:
            kept += 1
    return len(fresh), kept


def discover_boards(store, log=print) -> dict:
    found, healthy, errors = [], [], []
    for entry in section("companies") or []:
        jobs, err = boards.fetch_company(entry)
        if err:
            errors.append(f"{entry['name']}: {err}")
        else:
            healthy.append(entry["name"])
            found.extend(jobs)
        log(f"[ats] {entry['name']:<18} {len(jobs):>4} open" + (f"  ! {err}" if err else ""))
    new, kept = ingest(store, found)
    closed = store.mark_closed(boards.PLATFORM, healthy)
    disc = section("discovery")
    if disc.get("enabled"):
        jobs, errs = boards.discover_aggregators(disc.get("queries") or [], int(disc.get("max_per_query", 30)))
        errors += errs
        n2, k2 = ingest(store, jobs)
        new, kept = new + n2, kept + k2
        closed += store.close_stale([boards.AGGREGATOR], float(disc.get("expire_after_days", 4)))
    return {"new": new, "kept": kept, "closed": closed, "errors": errors}


def discover(name: str, ctx, store, log=print) -> dict:
    if name == "ats":
        result = discover_boards(store, log)
    else:
        jobs = sites.get(name).discover(ctx, store, log)
        new, kept = ingest(store, jobs)
        closed = store.close_stale([name], SEARCH_EXPIRY_DAYS)
        result = {"new": new, "kept": kept, "closed": closed, "errors": []}
    store.set_site(name, last_discover=datetime.now().isoformat(timespec="seconds"))
    store.log_event("discover", f"{result['new']} new ({result['kept']} pass filters), "
                                f"{result['closed']} closed", name)
    log(f"[{name}] {result['new']} new, {result['kept']} pass the filters, {result['closed']} closed")
    return result


def score_and_route(store, log=print) -> dict:
    summary = {"scored": 0}
    try:
        client = llm.client()
    except llm.LLMConfigError as e:
        # No usable model: jobs stay unscored (and so unrouted) until it is fixed.
        log(f"scoring unavailable: {e}")
        client = None
    if client is not None and store.unscored_count():
        summary = scoring.score_pending(store, client, candidate_summary(), log)
        if summary["scored"]:
            store.log_event("score", f"scored {summary['scored']} jobs")
    counts = router.route_pending(store)
    if any(counts.values()):
        log(f"routed: {counts['auto']} auto, {counts['review']} to review, "
            f"{counts['skip']} skipped, {counts['duplicate']} duplicates")
        store.log_event("route", f"{counts['auto']} auto · {counts['review']} review · "
                                 f"{counts['skip']} skip · {counts['duplicate']} duplicate")
    return {**summary, **counts}


def _short(job: dict) -> str:
    return f"{(job['title'] or '')[:55]} | {(job.get('company') or '')[:25]}"


def _maybe_rescore(store, job: dict, old_desc_len: int) -> bool:
    """A job routed to auto on a search-card snippet is re-scored once the full
    description is loaded, if the snippet was thin. Returns True to proceed."""
    if job["route"] != "auto" or job["status"] == "approved" or old_desc_len >= 400:
        return True
    if len(job.get("description") or "") < 400:
        return True
    try:
        client = llm.client()
        if client is None:
            return True
        got = scoring.score_one(store, client, candidate_summary(), job)
    except Exception:  # noqa: BLE001 - keep the first score
        return True
    if not got:
        return True
    score, reason, bd, risk = got
    score, reason = gates.apply_all(score, job["title"], bd, reason)
    store.save_score(job["id"], score, reason, bd, risk)
    job.update(score=score, reason=reason, scam_risk=risk)
    route, status, note = router.decide(job)
    if route == "auto":
        return True
    store.update(job["id"], route=route, status=status, note=f"re-scored on full posting: {note}")
    return False


def apply_round(ctx, store, name: str, limit: int | None = None, dry_run: bool = False, log=print) -> dict:
    """Work through one site's apply queue. Returns {"tally", "stop"} where stop
    is None | quota | cap | blocked | failures | logged_out."""
    mod = sites.get(name)
    cap = (section("daily_caps") or {}).get(name)
    max_fail = int(section("auto").get("max_consecutive_failures", 3))
    tally = {"applied": 0, "skipped": 0, "review": 0, "failed": 0, "needs_input": 0, "dry_run": 0}
    store.recover_interrupted(name)
    jobs = store.jobs_to_apply(name)
    log(f"\n[{name}] {len(jobs)} queued, {store.applied_today(name)} applied today" + (f" (cap {cap})" if cap else ""))
    if not jobs:
        return {"tally": tally, "stop": None}

    stop, fails_in_row, done = None, 0, 0
    page = ctx.new_page()
    try:
        if hasattr(mod, "logged_in"):
            ok = mod.logged_in(page)
            store.set_site(name, logged_in=int(ok))
            if not ok:
                log(f"[{name}] NOT LOGGED IN - run: python main.py login")
                return {"tally": tally, "stop": "logged_out"}

        for job in jobs:
            if cap and store.applied_today(name) >= cap:
                stop = "cap"
                break
            if limit and done >= limit:
                break
            prev_status = job["status"]
            old_len = len(job.get("description") or "")
            try:
                state, handle = mod.open_job(page, job)
            except Exception as e:  # noqa: BLE001
                log(f"   page did not load, retry later: {_short(job)} ({type(e).__name__})")
                continue
            if len(job.get("description") or "") > old_len:
                store.update(job["id"], description=job["description"][:9000])

            if state == "blocked":
                log(f"[{name}] site shows a human-verification check - pausing {name}")
                stop = "blocked"
                break
            flag = filters.red_flag(job.get("description"))
            if flag:
                store.update(job["id"], status="skipped", route="skip", note=flag)
                tally["skipped"] += 1
                continue
            if state == "already":
                store.update(job["id"], status="applied", note="already applied")
                continue
            if state == "closed":
                store.update(job["id"], status="closed", note="closed / no apply button")
                tally["skipped"] += 1
                continue
            if state == "external":
                store.update(job["id"], apply_kind="external", route="review", status="review",
                             note="apply on the company site yourself")
                tally["review"] += 1
                continue
            if state != "apply":
                continue
            if not _maybe_rescore(store, job, old_len):
                tally["skipped"] += 1
                continue

            log(f"\n>> [{name}] APPLY [{job['score']}] {_short(job)} | {job.get('location') or '-'}")
            sess = sites.Session(store, dry_run=dry_run, log=log)
            if name != "naukri":            # Naukri always sends the resume stored in your account
                sess.resume = tailor.resume_for(store, job, log)
            store.update(job["id"], status="applying", attempts=(job.get("attempts") or 0) + 1)
            try:
                status, note = mod.apply_job(page, job, handle, sess)
            except Exception as e:  # noqa: BLE001
                shot = screenshot(page, f"{name}_error_{job['id'].split(':')[-1]}")
                status, note = "failed", f"error: {str(e).splitlines()[0][:90]} [{shot}]"

            if status in ("quota", "blocked"):
                store.update(job["id"], status=prev_status)         # retry this one later
                log(f"   => STOPPED: {note}")
                stop = status
                break
            if status == "dry_run":
                store.update(job["id"], status=prev_status)
                store.record_application(job["id"], name, "dry_run", note, sess.qa, sess.screenshot)
                log(f"   => DRY RUN: {note}")
                tally["dry_run"] += 1
                done += 1
                continue

            store.update(job["id"], status=status, note=note)
            store.record_application(job["id"], name, status, note or "", sess.qa, sess.screenshot)
            tally[status] = tally.get(status, 0) + 1
            label = {"applied": "APPLIED", "needs_input": "WAITING - needs your answer", "failed": "FAILED"}[status]
            log(f"   => {label}{': ' + note if note and status != 'applied' else ''}")
            store.log_event(status, f"{_short(job)}" + (f" - {note}" if note and status != "applied" else ""),
                            name, job["id"])

            if status == "failed":
                fails_in_row += 1
                if fails_in_row >= max_fail:
                    log(f"[{name}] {max_fail} failures in a row - pausing (screenshots in data\\screens)")
                    stop = "failures"
                    break
            else:
                fails_in_row = 0
            if status == "applied":
                done += 1
                human.between_applications(log)
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
        log(f"[{name}] round done: " + ", ".join(f"{v} {k}" for k, v in tally.items() if v))
    return {"tally": tally, "stop": stop}
