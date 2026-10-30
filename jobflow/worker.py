"""The non-stop worker (python main.py start / auto).

Round-robin over the sites: search each one every few hours, score and route
what is new, then apply to up to `per_turn` jobs on that site before moving on.
A site pauses itself when it pushes back:

  site's daily limit / our daily cap  -> until tomorrow, 00:15-01:30
  human-verification check            -> 3 hours (+ notification)
  3 failures in a row                 -> 1 hour (the site probably changed)
  logged out                          -> 30 minutes (+ notification)

Any unexpected error is logged and the loop restarts after a minute.
"""
from __future__ import annotations

import random
import socket
import sys
import time
from datetime import date, datetime, timedelta

from . import notify, pipeline
from .config import DATA_DIR, section

LOCK_PORT = 47392
_lock: socket.socket | None = None


def single_instance() -> bool:
    """Only one worker at a time: hold a local port as a lock."""
    global _lock
    _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _lock.bind(("127.0.0.1", LOCK_PORT))
        return True
    except OSError:
        return False


class Tee:
    """Print to the console (if any) and append to data/worker.log."""

    def __init__(self, stream, path):
        self.stream, self.file = stream, open(path, "a", encoding="utf-8", buffering=1)

    def write(self, s):
        if self.stream:
            try:
                self.stream.write(s)
            except Exception:  # noqa: BLE001 - pythonw has no console
                pass
        self.file.write(s)

    def flush(self):
        if self.stream:
            try:
                self.stream.flush()
            except Exception:  # noqa: BLE001
                pass
        self.file.flush()


def _tomorrow() -> datetime:
    t = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return t + timedelta(minutes=random.randint(15, 90))


PAUSES = {
    "quota": _tomorrow,
    "cap": _tomorrow,
    "blocked": lambda: datetime.now() + timedelta(hours=3),
    "failures": lambda: datetime.now() + timedelta(hours=1),
    "logged_out": lambda: datetime.now() + timedelta(minutes=30),
    "error": lambda: datetime.now() + timedelta(minutes=30),
}


def paused_until(store, name: str) -> datetime | None:
    v = store.site(name).get("paused_until")
    if not v:
        return None
    t = datetime.fromisoformat(v)
    return t if t > datetime.now() else None


def pause(store, name: str, why: str, log=print) -> None:
    until = PAUSES[why]()
    store.set_site(name, paused_until=until.isoformat(timespec="seconds"), reason=why)
    store.log_event("pause", f"paused until {until:%d %b %H:%M} ({why})", name)
    log(f"[{name}] paused until {until:%d %b %H:%M} ({why})")
    if why in ("logged_out", "blocked"):
        notify.toast("JobFlow needs you",
                     f"{name}: {'log in again - python main.py login' if why == 'logged_out' else 'verification check shown'}")


def _discover_due(store, name: str) -> bool:
    last = store.site(name).get("last_discover")
    hours = float(section("auto").get("discover_every_hours", 4))
    return not last or datetime.now() - datetime.fromisoformat(last) > timedelta(hours=hours)


def _summary_due(store) -> bool:
    """The daily summary goes out once, in the evening."""
    if datetime.now().hour < 19:
        return False
    row = store.db.execute("SELECT 1 FROM runs WHERE kind = 'summary' AND date(ts, 'unixepoch', 'localtime') = ?",
                           (date.today().isoformat(),)).fetchone()
    return row is None


def cycle(store, ctx, names: list[str], log=print) -> int:
    """One pass over every site. Returns applications made."""
    per_turn = int(section("auto").get("per_turn", 5))
    applied = 0
    for name in names:
        if paused_until(store, name):
            continue
        try:
            if _discover_due(store, name):
                pipeline.discover(name, ctx, store, log)
                pipeline.score_and_route(store, log)
            res = pipeline.apply_round(ctx, store, name, limit=per_turn, log=log)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            log(f"[{name}] error: {type(e).__name__}: {msg[:150]}")
            if "closed" in msg.lower() or "Target page" in msg or "connect" in msg.lower():
                raise ConnectionError("browser lost") from e     # restart Chrome
            pause(store, name, "error", log)
            continue
        applied += res["tally"]["applied"]
        if res["stop"]:
            pause(store, name, res["stop"], log)
    return applied


def run(store, only: str | None = None) -> None:
    from .browser import connect

    if not single_instance():
        print("The worker is already running (python main.py status).")
        return
    sys.stdout = sys.stderr = Tee(sys.stdout, DATA_DIR / "worker.log")
    names = [only] if only else list(section("auto").get("platforms") or [])
    idle = float(section("auto").get("idle_minutes", 20))
    print(f"\n===== worker started {datetime.now():%Y-%m-%d %H:%M} | sites: {', '.join(names)} =====")
    pw = ctx = None
    while True:                     # never die
        try:
            if ctx is None:
                pw, ctx = connect()
            applied = cycle(store, ctx, names, print)
            pipeline.score_and_route(store, print)       # anything answered/approved meanwhile
            c = store.counts()
            print(f"\n----- {datetime.now():%H:%M} | applied today {store.applied_today()} | "
                  f"{c['review']} waiting for approval | {c['needs_input']} need an answer -----")
            if _summary_due(store):
                try:
                    notify.daily_summary(store)
                except Exception as e:  # noqa: BLE001
                    print(f"[email] {e}")
                store.log_run("summary", c)
            if applied == 0:
                print(f"nothing to apply right now - next check in {idle:.0f} min")
                time.sleep(idle * 60)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:  # noqa: BLE001
            print(f"[worker] {type(e).__name__}: {str(e)[:200]} - restarting in 60s")
            try:
                if pw:
                    pw.stop()
            except Exception:  # noqa: BLE001
                pass
            pw = ctx = None
            time.sleep(60)
    if pw:
        pw.stop()
