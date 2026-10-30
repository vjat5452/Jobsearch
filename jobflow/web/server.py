"""Dashboard: where the hybrid half happens.

The worker applies to easy-apply jobs on its own. Everything else lands here:
approve or skip it, answer the questions the bot could not answer from your
profile, and see exactly what was sent on your behalf (answers + screenshot).
It reads and writes the same SQLite file as the worker.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import ats, config, llm, router, sites, tailor
from ..resume import extract_text
from ..config import DB_PATH, ROOT, SCREENS_DIR, resume_path, section
from ..store import Store

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="JobFlow")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/screens", StaticFiles(directory=str(SCREENS_DIR)), name="screens")
store = Store(DB_PATH)          # thread-local connections inside


@app.get("/")
def index():
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/overview")
def overview():
    caps = section("daily_caps") or {}
    now = datetime.now()
    site_rows = []
    for name in sites.NAMES:
        st = store.site(name)
        paused = st.get("paused_until")
        auto_ok, auto_min, review_min = router.thresholds(name)
        site_rows.append({
            "name": name, "applied_today": store.applied_today(name), "cap": caps.get(name),
            "paused_until": paused if paused and datetime.fromisoformat(paused) > now else None,
            "reason": st.get("reason"), "logged_in": st.get("logged_in"),
            "last_discover": st.get("last_discover"),
            "auto": auto_ok, "auto_min": auto_min, "review_min": review_min,
        })
    from ..scoring import Budget
    budget = Budget(store, int(section("llm").get("daily_token_limit", 500_000)))
    return {"counts": store.counts(), "applied_today": store.applied_today(), "sites": site_rows,
            "questions": len(store.pending_questions()),
            "tokens": {"used": budget.used(), "limit": budget.daily_limit}}


@app.get("/api/platform/{name}")
def platform(name: str):
    """One site's page: its login/pause state, today's count against the cap,
    how many of its jobs are in each queue, and its buttons' run state."""
    if name not in SITE_TABS:
        raise HTTPException(404, "no such site")
    st = store.site(name)
    paused = st.get("paused_until")
    auto_ok, auto_min, review_min = router.thresholds(name)
    return {
        "name": name, "applied_today": store.applied_today(name),
        "cap": (section("daily_caps") or {}).get(name),
        "paused_until": paused if paused and datetime.fromisoformat(paused) > datetime.now() else None,
        "reason": st.get("reason"), "logged_in": st.get("logged_in"), "last_discover": st.get("last_discover"),
        "auto": auto_ok, "auto_min": auto_min, "review_min": review_min,
        "search": {k: v for k, v in (section(name) or {}).items() if k in ("keywords", "locations", "location")},
        "counts": store.counts(name),
        "actions": {a: _action_state(f"{a}-{name}") for a in SITE_ACTIONS},
        "login_waiting": _login_flag(name).exists(),
    }


@app.get("/api/jobs")
def jobs(status: str = "review", q: str = "", limit: int = 500, platform: str = ""):
    wanted = [s for s in status.split(",") if s] or None
    rows = store.jobs(wanted, q, limit, platform or None)
    for r in rows:
        try:
            r["breakdown"] = json.loads(r["breakdown"]) if r["breakdown"] else {}
        except ValueError:
            r["breakdown"] = {}
    return {"jobs": rows}


def _job_or_404(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return job


def _approvable(job: dict) -> str | None:
    """Why this job cannot be handed to the bot, or None if it can."""
    if job["apply_kind"] == "external" or job["platform"] not in sites.NAMES:
        return "this job can only be applied to on the company's site - open it"
    if job["status"] not in ("review", "skipped", "failed", "duplicate"):
        return f"job is {job['status']}"
    return None


def _approve(job: dict) -> None:
    store.update(job["id"], status="approved", route="review", note="approved by you")
    store.log_event("approve", f"{job['title']} | {job['company']}", job["platform"], job["id"])


@app.post("/api/jobs/{job_id}/approve")
def approve(job_id: str):
    job = _job_or_404(job_id)
    why = _approvable(job)
    if why:
        raise HTTPException(400, why)
    _approve(job)
    return {"ok": True}


class Ids(BaseModel):
    ids: list[str]


@app.post("/api/apply-all")
def apply_all(body: Ids):
    """The one button: hand every listed job the bot can apply to over to it.
    Jobs that need you (external links) are left in the list."""
    done, left = 0, 0
    for job_id in body.ids[:500]:
        job = store.get(job_id)
        if job and not _approvable(job):
            _approve(job)
            done += 1
        else:
            left += 1
    return {"approved": done, "left": left}


@app.post("/api/jobs/{job_id}/ats")
def job_ats(job_id: str):
    """Score your resume against this posting and, if coverage is low, make the
    edited copy now (the bot would do the same at apply time)."""
    job = _job_or_404(job_id)
    if not resume_path().exists():
        raise HTTPException(400, "upload your resume first (Setup tab)")
    msgs: list[str] = []
    path = tailor.resume_for(store, job, log=msgs.append)
    job = store.get(job_id)
    if job.get("ats_score") is None:
        ats.for_job(store, job, extract_text())
        job = store.get(job_id)
    record = json.loads(job["tailored"]) if job.get("tailored") else {}
    return {"ats": job.get("ats_score"), "ats_after": job.get("ats_after"), "edited": bool(path),
            "record": record, "log": [m.strip() for m in msgs]}


@app.get("/api/jobs/{job_id}/resume")
def job_resume(job_id: str):
    """The resume the bot sends for this job: the edited copy if one was made, else yours."""
    job = _job_or_404(job_id)
    path = tailor.RESUMES_DIR / job["resume_file"] if job.get("resume_file") else resume_path()
    if not path.exists():
        raise HTTPException(404, "no resume uploaded yet")
    return FileResponse(str(path), media_type="application/pdf", filename=path.name,
                        content_disposition_type="inline")


@app.post("/api/jobs/{job_id}/skip")
def skip(job_id: str):
    job = _job_or_404(job_id)
    store.update(job_id, status="skipped", route="skip", note="skipped by you")
    store.log_event("skip", f"{job['title']} | {job['company']}", job["platform"], job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/manual")
def manual(job_id: str):
    """You applied yourself (external / company site)."""
    job = _job_or_404(job_id)
    store.update(job_id, status="manual", note="applied by you")
    store.record_application(job_id, job["platform"], "manual", "applied by you")
    return {"ok": True}


@app.get("/api/questions")
def questions():
    rows = store.pending_questions()
    for r in rows:
        r["options"] = json.loads(r["options"] or "[]")
    return {"questions": rows}


class Answer(BaseModel):
    answer: str


@app.post("/api/questions/{key}")
def answer(key: str, body: Answer):
    if not body.answer.strip():
        raise HTTPException(400, "empty answer")
    released = store.set_answer(key, body.answer.strip())
    store.log_event("answer", f"answered: {key[:80]} - {released} job(s) back in the queue")
    return {"released": released}


@app.get("/api/applications")
def applications(limit: int = 100, platform: str = ""):
    rows = store.applications(limit, platform or None)
    for r in rows:
        r["qa"] = json.loads(r["qa"] or "[]")
    return {"applications": rows}


@app.post("/api/sites/{name}/resume")
def resume_site(name: str):
    if name not in sites.NAMES:
        raise HTTPException(404, "no such site")
    store.set_site(name, paused_until=None, reason=None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Setup: key, resume, profile - and buttons for the CLI commands
# ---------------------------------------------------------------------------

def _key_env() -> str:
    cfg = section("llm")
    return cfg.get("key_env") or (llm.ENDPOINTS.get(cfg.get("provider", "groq")) or (None, ""))[1] or ""


def _worker_running() -> bool:
    import socket
    from ..worker import LOCK_PORT

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _reset_resume_work(all_jobs: bool = True) -> None:
    """A new resume or profile changes ATS coverage and what may be added."""
    cols = "resume_file = NULL, tailored = NULL, ats_after = NULL" + (", ats_score = NULL" if all_jobs else "")
    store.db.execute(f"UPDATE jobs SET {cols} WHERE status NOT IN ('applied', 'applying')")
    store.db.commit()


@app.get("/api/setup")
def setup():
    import os

    config.reload()                     # pick up a profile written by "Draft from resume"
    real_profile = config.CONFIG_DIR / "profile.yaml"
    path = resume_path()
    env = _key_env()
    return {
        "provider": section("llm").get("provider", "groq"), "key_env": env,
        "has_key": bool(env and os.environ.get(env)) or not env,
        "resume": path.name if path.exists() else None,
        "profile_exists": real_profile.exists(),
        "profile": (real_profile if real_profile.exists()
                    else config.CONFIG_DIR / "profile.example.yaml").read_text(encoding="utf-8"),
        "profile_name": (config.PROFILE.get("personal") or {}).get("full_name"),
        "skills": len(config.PROFILE.get("skill_years") or {}),
        "worker": _worker_running(),
        "actions": {k: _action_state(k) for k in ACTIONS},
    }


class Key(BaseModel):
    key: str


@app.post("/api/setup/key")
def save_key(body: Key):
    key = body.key.strip()
    env = _key_env()
    if not env:
        raise HTTPException(400, "this provider needs no key")
    if len(key) < 10 or " " in key:
        raise HTTPException(400, "that does not look like an API key")
    config.write_secret(env, key)
    llm._client = None                  # next call builds a client with the new key
    ok, detail = True, ""
    try:
        c = llm.LLM()
        if c.base_url:
            import requests
            r = requests.get(f"{c.base_url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
            ok, detail = r.status_code == 200, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}"
    return {"saved": True, "valid": ok, "detail": detail}


@app.post("/api/setup/resume")
async def upload_resume(file: UploadFile = File(...)):
    name = Path(file.filename or "resume.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "upload a PDF - the in-place editing works on PDFs")
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "that file is not a PDF")
    folder = ROOT / "resume"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.pdf"):
        old.unlink()
    dest = folder / name
    dest.write_bytes(data)
    config.set_resume_path(dest)
    config.reload()
    _reset_resume_work()
    try:
        words = len(extract_text(dest).split())
    except Exception:  # noqa: BLE001
        words = 0
    return {"saved": name, "words": words,
            "warning": "" if words > 50 else "almost no text found - is it a scanned image? ATS systems can't read those either"}


class Profile(BaseModel):
    text: str


@app.post("/api/setup/profile")
def save_profile(body: Profile):
    import yaml

    try:
        data = yaml.safe_load(body.text)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"not valid YAML: {str(e)[:200]}") from e
    if not isinstance(data, dict) or "personal" not in data:
        raise HTTPException(400, "the profile must keep its sections (personal, education, experience, ...)")
    (config.CONFIG_DIR / "profile.yaml").write_text(body.text, encoding="utf-8")
    config.reload()
    _reset_resume_work(all_jobs=False)
    return {"saved": True}


# The CLI commands, runnable from the dashboard. Each runs as its own process
# (fresh config, and a crash cannot take the dashboard down).
ACTIONS = {
    "init": ["init", "--force"], "score": ["score"], "discover": ["discover", "--only", "ats"],
    "recap": ["recap"], "start": ["start"], "stop": ["stop"],
}
# The sites with their own tab, and the buttons each tab has.
SITE_TABS = ("linkedin", "naukri", "unstop")
SITE_ACTIONS = {
    "login": ["login", "--only"],                         # Chrome on screen until "I'm logged in"
    "search": ["discover", "--only"],
    "dryrun": ["apply", "--dry-run", "--limit", "2", "--only"],   # fills forms, never submits
    "apply": ["apply", "--limit", "5", "--only"],
}
for _s in SITE_TABS:
    for _a, _argv in SITE_ACTIONS.items():
        ACTIONS[f"{_a}-{_s}"] = [*_argv, _s]
_procs: dict = {}


def _login_flag(name: str) -> Path:
    """While this file exists, `main.py login --only <name>` keeps Chrome on screen."""
    return config.DATA_DIR / f"login-{name}.wait"


@app.post("/api/platform/{name}/login-done")
def login_done(name: str):
    if name not in SITE_TABS:
        raise HTTPException(404, "no such site")
    _login_flag(name).unlink(missing_ok=True)
    return {"ok": True}


def _action_log(name: str) -> Path:
    d = config.DATA_DIR / "actions"
    d.mkdir(exist_ok=True)
    return d / f"{name}.log"


def _action_state(name: str) -> dict:
    p = _procs.get(name)
    log = _action_log(name)
    tail = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-12:] if log.exists() else []
    return {"running": bool(p and p.poll() is None), "exit": None if not p else p.poll(), "log": tail}


@app.post("/api/actions/{name}")
def run_action(name: str):
    import subprocess
    import sys

    if name not in ACTIONS:
        raise HTTPException(404, "no such action")
    if _action_state(name)["running"]:
        raise HTTPException(400, f"{name} is already running")
    if name.startswith("login-"):
        _login_flag(name.split("-", 1)[1]).touch()
    out = open(_action_log(name), "w", encoding="utf-8")
    _procs[name] = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "main.py"), *ACTIONS[name]], cwd=str(ROOT), stdout=out,
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=0x08000000,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    return {"started": name}


@app.get("/api/actions/{name}")
def action_status(name: str):
    if name not in ACTIONS:
        raise HTTPException(404, "no such action")
    return _action_state(name)


@app.get("/api/events")
async def events(after: int = 0):
    """Server-Sent Events: the worker's activity log as it happens."""
    async def stream():
        last = after or max(0, store.last_event_id() - 30)
        while True:
            rows = await asyncio.to_thread(store.events_since, last)
            for r in rows:
                last = r["id"]
                yield f"data: {json.dumps(r)}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
