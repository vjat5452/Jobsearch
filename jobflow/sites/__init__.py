"""Browser platforms. Each module provides:

    PLATFORM                          name used in the database
    discover(ctx, store, log) -> list[dict]   jobs found (models.make_job shape)
    logged_in(page) -> bool           (optional) is the session still logged in
    open_job(page, job) -> (state, handle)
        state: "apply" | "already" | "external" | "closed" | "blocked"
        sets job["description"] to the full posting text
    apply_job(page, job, handle, sess) -> (status, note)
        status: "applied" | "needs_input" | "failed" | "quota" | "blocked" | "dry_run"
"""
from __future__ import annotations

from importlib import import_module

NAMES = ("linkedin", "naukri", "indeed", "glassdoor", "unstop", "ats")


def get(name: str):
    if name not in NAMES:
        raise KeyError(name)
    return import_module(f".{'ats_form' if name == 'ats' else name}", __name__)


class Session:
    """What an apply run carries: the store, the Q/A it gave (kept as an audit
    trail), whether it may press the final submit, and where to log."""

    def __init__(self, store, dry_run: bool = False, log=print):
        self.store, self.dry_run, self.log = store, dry_run, log
        self.qa: list[dict] = []
        self.screenshot = ""        # proof of the final page, kept with the application record
        self.resume = None          # resume tailored for this job; None = your base resume
