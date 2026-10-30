"""SQLite state: what has been seen, scored, routed and applied to.

Two jobs share this file at once - the worker and the dashboard - so it runs in
WAL mode and every thread gets its own connection (sqlite3 connections are bound
to the thread that opened them).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    source      TEXT,
    apply_kind  TEXT NOT NULL,
    fingerprint TEXT,
    title       TEXT NOT NULL,
    company     TEXT,
    location    TEXT,
    salary      TEXT,
    experience  TEXT,
    url         TEXT,
    apply_url   TEXT,
    description TEXT,
    posted_at   REAL,
    domain      TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    closed_at   REAL,
    score       INTEGER,
    breakdown   TEXT,
    reason      TEXT,
    scam_risk   TEXT,
    route       TEXT,          -- auto | review | skip
    status      TEXT NOT NULL, -- new | queued | review | approved | applying | applied |
                               -- needs_input | failed | skipped | duplicate | closed | manual
    note        TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    applied_at  TEXT,
    notified    INTEGER NOT NULL DEFAULT 0,
    ats_keywords TEXT,         -- JSON {"must": [...], "nice": [...]} from the posting
    ats_score   INTEGER,       -- your base resume's keyword coverage, 0-100
    ats_after   INTEGER,       -- coverage of the edited resume, when one was made
    resume_file TEXT,          -- the resume edited for this job (data/resumes/...)
    tailored    TEXT           -- JSON: the line edits made, and keywords still missing
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_fp ON jobs(fingerprint);
CREATE TABLE IF NOT EXISTS questions (
    key TEXT PRIMARY KEY, question TEXT, kind TEXT, options TEXT,
    answer TEXT, created_at TEXT, answered_at TEXT
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, platform TEXT, at TEXT,
    result TEXT, note TEXT, qa TEXT, screenshot TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, platform TEXT,
    job_id TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS site_state (
    platform TEXT PRIMARY KEY, paused_until TEXT, reason TEXT,
    last_discover TEXT, logged_in INTEGER
);
CREATE TABLE IF NOT EXISTS spend (day TEXT PRIMARY KEY, tokens INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs (ts INTEGER PRIMARY KEY, kind TEXT, summary TEXT);
"""

# Statuses that mean "we have not acted on this yet", so a closure or a
# duplicate can still take it out of the pipeline.
OPEN_STATUSES = ("new", "queued", "review", "approved", "needs_input")
# Statuses that mean a copy of this role is already being or has been handled.
HANDLED_STATUSES = ("queued", "review", "approved", "applying", "applied", "needs_input", "manual")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()
        self.last_upsert_ts = time.time()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("resume_file", "TEXT"), ("tailored", "TEXT"), ("ats_keywords", "TEXT"),
                         ("ats_score", "INTEGER"), ("ats_after", "INTEGER")):
            if col not in have:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    # -- ingest ----------------------------------------------------------------
    def upsert(self, jobs: list[dict]) -> list[dict]:
        """Insert jobs, returning only those never seen before. A known job is
        marked seen again; one that had closed is reopened, not duplicated."""
        now = time.time()
        self.last_upsert_ts = now
        fresh = []
        for j in jobs:
            row = self.db.execute("SELECT status, closed_at, description FROM jobs WHERE id = ?",
                                  (j["id"],)).fetchone()
            if row:
                desc = j.get("description") or ""
                self.db.execute(
                    """UPDATE jobs SET last_seen = ?, closed_at = NULL,
                         status = CASE WHEN status = 'closed' THEN 'new' ELSE status END,
                         description = CASE WHEN LENGTH(COALESCE(description,'')) < ? THEN ? ELSE description END
                       WHERE id = ?""",
                    (now, len(desc), desc, j["id"]))
            else:
                self.db.execute(
                    """INSERT INTO jobs (id, platform, source, apply_kind, fingerprint, title, company,
                         location, salary, experience, url, apply_url, description, posted_at, domain,
                         first_seen, last_seen, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new')""",
                    (j["id"], j["platform"], j.get("source"), j["apply_kind"], j.get("fingerprint"),
                     j["title"], j.get("company"), j.get("location"), j.get("salary"),
                     j.get("experience"), j.get("url"), j.get("apply_url"),
                     (j.get("description") or "")[:9000], j.get("posted_at"), j.get("domain"), now, now))
                fresh.append(j)
        self.db.commit()
        return fresh

    def skip_new(self, job_id: str, note: str) -> None:
        """A freshly inserted job that failed the cheap filters."""
        self.update(job_id, status="skipped", route="skip", note=note)

    def mark_closed(self, platform: str, companies: list[str], run_ts: float | None = None) -> int:
        """A board job missing from a board that fetched cleanly is closed. Only
        companies fetched successfully this run - one network blip must not close
        a whole board."""
        if not companies:
            return 0
        run_ts = run_ts if run_ts is not None else self.last_upsert_ts
        marks = ",".join("?" * len(companies))
        cur = self.db.execute(
            f"""UPDATE jobs SET closed_at = ?,
                  status = CASE WHEN status IN {OPEN_STATUSES} THEN 'closed' ELSE status END
                WHERE platform = ? AND company IN ({marks}) AND closed_at IS NULL AND last_seen < ?""",
            (run_ts, platform, *companies, run_ts))
        self.db.commit()
        return cur.rowcount

    def close_stale(self, platforms: list[str], max_age_days: float) -> int:
        """Searches and aggregators never prove a job is gone - absence from one
        search means only that it did not surface. Age is the usable signal."""
        if not platforms:
            return 0
        cutoff = time.time() - max_age_days * 86400
        marks = ",".join("?" * len(platforms))
        cur = self.db.execute(
            f"""UPDATE jobs SET closed_at = ?, status = 'closed'
                WHERE platform IN ({marks}) AND closed_at IS NULL AND last_seen < ?
                  AND status IN {OPEN_STATUSES}""",
            (time.time(), *platforms, cutoff))
        self.db.commit()
        return cur.rowcount

    def refilter(self, prefilter) -> int:
        """Re-run the cheap filters over jobs not yet acted on (after the rules change)."""
        n = 0
        for row in self.db.execute(
                "SELECT * FROM jobs WHERE status IN ('new','queued','review')").fetchall():
            reason = prefilter(dict(row))
            if reason:
                self.update(row["id"], status="skipped", route="skip", note=reason)
                n += 1
        return n

    # -- generic -----------------------------------------------------------------
    def get(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        if fields.get("status") == "applied":
            fields.setdefault("applied_at", _now_iso())
        if "breakdown" in fields and isinstance(fields["breakdown"], dict):
            fields["breakdown"] = json.dumps(fields["breakdown"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
        self.db.commit()

    # -- scoring -----------------------------------------------------------------
    def unscored(self, limit: int = 5000) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE score IS NULL AND status = 'new' AND closed_at IS NULL "
            "ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def unscored_count(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE score IS NULL AND status = 'new' AND closed_at IS NULL"
        ).fetchone()[0]

    def save_score(self, job_id: str, score: int, reason: str, breakdown: dict | None,
                   scam_risk: str = "low") -> None:
        self.update(job_id, score=score, reason=reason, breakdown=breakdown or {},
                    scam_risk=scam_risk or "low")

    def unrouted(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE status = 'new' AND score IS NOT NULL AND closed_at IS NULL "
            "ORDER BY score DESC").fetchall()
        return [dict(r) for r in rows]

    def siblings(self, fingerprint: str, exclude_id: str) -> list[dict]:
        """Other copies of the same role (same company + title) on any site."""
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE fingerprint = ? AND id != ?", (fingerprint, exclude_id)).fetchall()
        return [dict(r) for r in rows]

    # -- applying ----------------------------------------------------------------
    def jobs_to_apply(self, platform: str) -> list[dict]:
        """Approved jobs first (you asked for them), then auto jobs by score."""
        rows = self.db.execute(
            """SELECT * FROM jobs WHERE platform = ? AND status IN ('queued','approved')
                 AND closed_at IS NULL
               ORDER BY status = 'approved' DESC, COALESCE(score,0) DESC, first_seen DESC""",
            (platform,)).fetchall()
        return [dict(r) for r in rows]

    def recover_interrupted(self, platform: str) -> int:
        """A job left 'applying' means the run died mid-form (crash, PC slept).
        Put it back in its queue; the site shows it as applied if it went through."""
        cur = self.db.execute(
            """UPDATE jobs SET status = CASE route WHEN 'auto' THEN 'queued' ELSE 'approved' END
               WHERE platform = ? AND status = 'applying'""", (platform,))
        self.db.commit()
        return cur.rowcount

    def applied_today(self, platform: str | None = None) -> int:
        today = f"{date.today().isoformat()}%"
        if platform:
            return self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE platform = ? AND status = 'applied' AND applied_at LIKE ?",
                (platform, today)).fetchone()[0]
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'applied' AND applied_at LIKE ?",
            (today,)).fetchone()[0]

    def record_application(self, job_id: str, platform: str, result: str, note: str = "",
                           qa: list | None = None, screenshot: str = "") -> None:
        self.db.execute(
            "INSERT INTO applications (job_id, platform, at, result, note, qa, screenshot) VALUES (?,?,?,?,?,?,?)",
            (job_id, platform, _now_iso(), result, note or "", json.dumps(qa or []), screenshot or ""))
        self.db.commit()

    def applications(self, limit: int = 100, platform: str | None = None) -> list[dict]:
        where, params = ("WHERE a.platform = ?", (platform,)) if platform else ("", ())
        rows = self.db.execute(
            f"""SELECT a.*, j.title, j.company, j.location, j.score, j.url, j.domain
               FROM applications a LEFT JOIN jobs j ON j.id = a.job_id
               {where} ORDER BY a.id DESC LIMIT ?""", (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    # -- review queue --------------------------------------------------------------
    def jobs(self, status: list[str] | None = None, q: str = "", limit: int = 500,
             platform: str | None = None) -> list[dict]:
        where, params = ["closed_at IS NULL"], []
        if platform:
            where.append("platform = ?")
            params.append(platform)
        if status:
            where.append(f"status IN ({','.join('?' * len(status))})")
            params += status
        for w in q.lower().split()[:6]:
            where.append("(LOWER(title) LIKE ? OR LOWER(company) LIKE ? OR LOWER(location) LIKE ?"
                         " OR LOWER(COALESCE(description,'')) LIKE ?)")
            params += [f"%{w}%"] * 4
        rows = self.db.execute(
            f"""SELECT id, platform, apply_kind, title, company, location, salary, experience, url,
                       apply_url, domain, score, breakdown, reason, scam_risk, route, status, note,
                       first_seen, applied_at, resume_file, tailored, ats_score, ats_after
                FROM jobs WHERE {' AND '.join(where)}
                ORDER BY COALESCE(score,-1) DESC, first_seen DESC LIMIT ?""",
            (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    # -- questions ------------------------------------------------------------------
    def get_answer(self, key: str) -> str | None:
        row = self.db.execute("SELECT answer FROM questions WHERE key = ?", (key,)).fetchone()
        return row["answer"] if row and row["answer"] is not None else None

    def save_question(self, key: str, question: str, kind: str, options) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO questions (key, question, kind, options, created_at) VALUES (?,?,?,?,?)",
            (key, question, kind, json.dumps(options or []), _now_iso()))
        self.db.commit()

    def set_answer(self, key: str, answer: str) -> int:
        """Store an answer and send every job blocked on it back to the apply
        queue it came from. Returns how many jobs were released."""
        self.db.execute("UPDATE questions SET answer = ?, answered_at = ? WHERE key = ?",
                        (answer, _now_iso(), key))
        cur = self.db.execute(
            """UPDATE jobs SET status = CASE route WHEN 'auto' THEN 'queued' ELSE 'approved' END,
                               note = NULL
               WHERE status = 'needs_input' AND note = ?""", (key,))
        self.db.commit()
        return cur.rowcount

    def pending_questions(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT q.*, (SELECT COUNT(*) FROM jobs j WHERE j.status = 'needs_input' AND j.note = q.key)
                      AS blocked
               FROM questions q WHERE q.answer IS NULL ORDER BY blocked DESC, created_at""").fetchall()
        return [dict(r) for r in rows]

    # -- activity log (the dashboard tails this) -------------------------------------
    def log_event(self, kind: str, message: str, platform: str = "", job_id: str = "") -> None:
        self.db.execute("INSERT INTO events (ts, kind, platform, job_id, message) VALUES (?,?,?,?,?)",
                        (time.time(), kind, platform, job_id, message[:500]))
        self.db.commit()

    def events_since(self, last_id: int, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                               (last_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def last_event_id(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    # -- per-site state ----------------------------------------------------------------
    def site(self, platform: str) -> dict:
        row = self.db.execute("SELECT * FROM site_state WHERE platform = ?", (platform,)).fetchone()
        return dict(row) if row else {"platform": platform, "paused_until": None, "reason": None,
                                      "last_discover": None, "logged_in": None}

    def set_site(self, platform: str, **fields) -> None:
        cur = self.site(platform)
        cur.update(fields)
        self.db.execute(
            """INSERT INTO site_state (platform, paused_until, reason, last_discover, logged_in)
               VALUES (?,?,?,?,?)
               ON CONFLICT(platform) DO UPDATE SET paused_until = excluded.paused_until,
                 reason = excluded.reason, last_discover = excluded.last_discover,
                 logged_in = excluded.logged_in""",
            (platform, cur["paused_until"], cur["reason"], cur["last_discover"], cur["logged_in"]))
        self.db.commit()

    # -- reporting ----------------------------------------------------------------------
    def status_counts(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT platform, status, COUNT(*) n FROM jobs GROUP BY 1, 2 ORDER BY 1, 3 DESC").fetchall()
        return [dict(r) for r in rows]

    def counts(self, platform: str | None = None) -> dict:
        where, params = ("WHERE platform = ?", (platform,)) if platform else ("", ())
        row = self.db.execute(
            f"""SELECT SUM(status = 'review') review, SUM(status = 'queued') queued,
                      SUM(status = 'approved') approved, SUM(status = 'needs_input') needs_input,
                      SUM(status = 'applied') applied, SUM(status = 'failed') failed,
                      SUM(closed_at IS NULL) open, COUNT(*) total
               FROM jobs {where}""", params).fetchone()
        return {k: row[k] or 0 for k in row.keys()}

    def log_run(self, kind: str, summary: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)",
                        (int(time.time()), kind, json.dumps(summary)))
        self.db.commit()
