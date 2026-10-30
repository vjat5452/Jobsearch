"""JobFlow - find jobs everywhere, rank them against your resume, apply on your behalf.

  python main.py init              draft config/profile.yaml from your resume (then check it!)
  python main.py login             show the bot's Chrome to log in to each site, Enter hides it
  python main.py start             start the worker hidden in the background
  python main.py stop              stop the worker and its Chrome
  python main.py status            running? today's numbers, pauses, last log lines
  python main.py web               dashboard: approve, answer questions, see what was sent
  python main.py auto              the worker, in this window

  python main.py discover [--only SITE]       search for new jobs
  python main.py score                        score + route everything unscored
  python main.py apply [--only SITE] [--dry-run]   apply to queued/approved jobs once
  python main.py run [--only SITE]            discover + score + apply once
  python main.py questions                    answer what the bot could not answer from your profile
  python main.py report                       what was applied / waiting / skipped
  python main.py recap                        re-apply the seniority/relevance rules (no tokens)
  python main.py test-email                   check SMTP

SITE: linkedin, naukri, indeed, glassdoor, unstop, ats
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from jobflow import notify, pipeline, router, scoring, sites
from jobflow.config import CONFIG_DIR, DATA_DIR, DB_PATH, section
from jobflow.store import Store

ROOT = Path(__file__).resolve().parent
NO_WINDOW = 0x08000000


def _sites(only):
    return [only] if only else list(section("auto").get("platforms") or sites.NAMES)


def _ps(cmd: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                          creationflags=NO_WINDOW).stdout.strip()


def _worker_pids() -> list[int]:
    out = _ps("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*main.py*auto*' -and "
              "$_.Name -like 'python*' } | ForEach-Object { $_.ProcessId }")
    return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]


# --------------------------------------------------------------------------- setup
def cmd_init(store, args):
    from jobflow import llm
    from jobflow.resume import extract_text

    target = CONFIG_DIR / "profile.yaml"
    if target.exists() and not args.force:
        sys.exit(f"{target} already exists. Use --force to redraft it (your edits would be lost).")
    for name in ("settings",):
        real = CONFIG_DIR / f"{name}.yaml"
        if not real.exists():
            real.write_text((CONFIG_DIR / f"{name}.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            print(f"created {real} - edit it (resume_path, keywords, companies)")
    template = (CONFIG_DIR / "profile.example.yaml").read_text(encoding="utf-8")
    draft = llm.draft_profile(extract_text(), template)
    target.write_text(draft + "\n", encoding="utf-8")
    print(f"Drafted {target}.\nEvery line marked  # CONFIRM  was inferred - check them all.\n"
          f"Everything JobFlow writes on an application comes from this file.")


def cmd_login(store, args):
    from jobflow.browser import LOGIN_PAGES, connect, place

    pw, ctx = connect(show=True)
    opened = []
    for name, url in LOGIN_PAGES.items():
        if args.only and name != args.only:
            continue
        p = ctx.new_page()
        opened.append(p)
        try:
            p.goto(url, wait_until="commit", timeout=30000)
        except Exception:  # noqa: BLE001
            pass
    flag = DATA_DIR / f"login-{args.only}.wait"
    if args.only and flag.exists():
        # Started from the dashboard: no terminal to press Enter in. The
        # dashboard's "I'm logged in" button removes the flag (or 20 min pass).
        print(f"Chrome is on screen with the {args.only} login page. Log in, then press "
              "\"I'm logged in\" in the dashboard.", flush=True)
        end = time.time() + 20 * 60
        while flag.exists() and time.time() < end:
            time.sleep(1)
        flag.unlink(missing_ok=True)
    else:
        input("\nThe bot's Chrome is on screen with each site's login page.\n"
              "Log in yourself (sites already logged in just show your account),\n"
              "then press Enter here to hide the window again... ")
    for p in opened:
        try:
            p.close()
        except Exception:  # noqa: BLE001
            pass
    for name in [args.only] if args.only else LOGIN_PAGES:
        store.set_site(name, logged_in=None, paused_until=None, reason=None)
    place(ctx, show=False)
    pw.stop()
    print("Hidden again. Logins are saved in .chrome-profile.")


def cmd_start(store, args):
    if _worker_pids():
        print("The worker is already running.  (python main.py status)")
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw if pythonw.exists() else sys.executable)
    subprocess.Popen([exe, str(ROOT / "main.py"), "auto"], cwd=str(ROOT),
                     creationflags=0x00000008 | NO_WINDOW | 0x00000200, close_fds=True)  # detached, no window
    time.sleep(3)
    print("Worker started in the background. Check on it: python main.py status  |  dashboard: python main.py web")


def cmd_stop(store, args):
    from jobflow.browser import kill_bot_chrome

    pids = _worker_pids()
    for p in pids:
        _ps(f"Stop-Process -Id {p} -Force -ErrorAction SilentlyContinue")
    kill_bot_chrome()
    print(f"Stopped the worker ({len(pids)} process) and its Chrome." if pids
          else "Worker was not running; its Chrome is closed.")


def cmd_status(store, args):
    from jobflow.worker import paused_until

    print("RUNNING in the background" if _worker_pids() else "NOT running   (python main.py start)")
    caps = section("daily_caps") or {}
    for name in sites.NAMES:
        p = paused_until(store, name)
        st = store.site(name)
        print(f"  {name:<10} applied today {store.applied_today(name):>3}/{caps.get(name) or '-':<4}"
              + (f"  paused until {p:%d %b %H:%M} ({st['reason']})" if p else "")
              + ("  NOT LOGGED IN" if st.get("logged_in") == 0 else ""))
    c = store.counts()
    print(f"\n{c['review']} waiting for your approval · {c['needs_input']} need an answer · "
          f"{c['queued']} queued to auto-apply · {c['applied']} applied all time")
    log = DATA_DIR / "worker.log"
    if log.exists():
        lines = [ln for ln in log.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
        print("\nLast activity (data\\worker.log):")
        for ln in lines[-15:]:
            print("   " + ln[:150])


# --------------------------------------------------------------------------- one-shot stages
def _with_browser(names, fn, always=False):
    """Only open Chrome when a stage needs it (board and Unstop searches do not;
    applying always does)."""
    need = always or any(n not in ("ats", "unstop") for n in names)
    pw = ctx = None
    if need:
        from jobflow.browser import connect
        pw, ctx = connect()
    try:
        return fn(ctx)
    finally:
        if pw:
            pw.stop()


def cmd_discover(store, args):
    names = _sites(args.only)

    def go(ctx):
        for n in names:
            try:
                pipeline.discover(n, ctx, store)
            except Exception as e:  # noqa: BLE001
                print(f"[{n}] search failed: {type(e).__name__}: {str(e)[:120]}")
    _with_browser(names, go)


def cmd_score(store, args):
    r = pipeline.score_and_route(store)
    print(f"{r.get('scored', 0)} scored · {r.get('auto', 0)} auto · {r.get('review', 0)} to review · "
          f"{r.get('skip', 0)} skipped · {r.get('duplicate', 0)} duplicates")


def cmd_apply(store, args):
    names = _sites(args.only)
    if args.dry_run:
        print("DRY RUN: forms are filled but never submitted.\n")
    _with_browser(names, lambda ctx: [
        pipeline.apply_round(ctx, store, n, limit=args.limit, dry_run=args.dry_run) for n in names],
        always=True)
    pending = store.pending_questions()
    if pending:
        print(f"\n{len(pending)} question(s) need your answer -> python main.py questions (or the dashboard)")


def cmd_run(store, args):
    cmd_discover(store, args)
    cmd_score(store, args)
    cmd_apply(store, args)


def cmd_questions(store, args):
    pending = store.pending_questions()
    if not pending:
        print("No pending questions.")
        return
    print("Answer truthfully - each answer is reused for every future application.\n"
          "Press Enter to skip a question.\n")
    for q in pending:
        opts = json.loads(q["options"] or "[]")
        print(f"[{q['blocked']} job(s) waiting] {q['question']}")
        for i, o in enumerate(opts, 1):
            print(f"   {i}. {o}")
        ans = input("   > ").strip()
        if not ans:
            continue
        if opts and ans.isdigit() and 1 <= int(ans) <= len(opts):
            ans = opts[int(ans) - 1]
        n = store.set_answer(q["key"], ans)
        print(f"   saved{f' - {n} job(s) back in the queue' if n else ''}\n")


def cmd_report(store, args):
    print("Jobs by site and status:")
    for r in store.status_counts():
        print(f"   {r['platform']:10} {r['status']:12} {r['n']}")
    print("\nRecent applications:")
    for a in store.applications(30):
        print(f"   {a['at']}  {a['platform']:9} {a['result']:9} [{a['score']}] {a['title']} - {a['company']}")
    print(f"\nApplied today: {store.applied_today()} · pending questions: {len(store.pending_questions())}")


def cmd_recap(store, args):
    n = scoring.recap(store)
    print(f"{n} scores capped (senior title or wrong field). No tokens used.")
    # Jobs not yet acted on are re-routed with their corrected scores.
    store.db.execute("UPDATE jobs SET status = 'new', route = NULL, note = NULL "
                     "WHERE status IN ('queued','review') AND score IS NOT NULL")
    store.db.commit()
    c = router.route_pending(store)
    print(f"re-routed: {c['auto']} auto, {c['review']} review, {c['skip']} skip, {c['duplicate']} duplicate")


def cmd_web(store, args):
    import uvicorn

    port = args.port or int(section("web").get("port", 8765))
    print(f"Dashboard: http://127.0.0.1:{port}")
    uvicorn.run("jobflow.web.server:app", host="127.0.0.1", port=port, log_level="warning")


def cmd_test_email(store, args):
    sent = notify.email("JobFlow - test", "<h1>SMTP works</h1><p>Daily summaries will arrive here.</p>",
                        "SMTP works.")
    print("Test email sent." if sent else "email.enabled is false in settings.yaml.")


COMMANDS = {
    "init": cmd_init, "login": cmd_login, "start": cmd_start, "stop": cmd_stop, "status": cmd_status,
    "discover": cmd_discover, "score": cmd_score, "apply": cmd_apply, "run": cmd_run,
    "questions": cmd_questions, "report": cmd_report, "recap": cmd_recap, "web": cmd_web,
    "test-email": cmd_test_email, "auto": None,
}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobflow", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=list(COMMANDS))
    ap.add_argument("--only", choices=list(sites.NAMES))
    ap.add_argument("--dry-run", action="store_true", help="fill forms but never submit")
    ap.add_argument("--limit", type=int, default=None, help="max applications per site this run")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    store = Store(DB_PATH)
    if args.command == "auto":
        from jobflow import worker
        worker.run(store, args.only)
        return
    if args.command not in ("init", "login", "start", "stop", "status", "web", "test-email"):
        from jobflow import filters
        n = store.refilter(filters.prefilter)
        if n:
            print(f"{n} queued jobs no longer pass the filters - skipped")
    COMMANDS[args.command](store, args)


if __name__ == "__main__":
    main()
