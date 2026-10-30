"""Batch scoring under a daily token budget.

* One request scores 25 jobs, so per-call overhead is paid once per batch.
* Replies are positional arrays, not JSON objects - no repeated key names in the
  output, which is billed like input.
* The system message (rules + candidate) is byte-identical across batches, so
  providers that cache prompt prefixes can bill it at a discount.
* The budget is checked before every request, and actual spend is recorded
  after each one, so an expensive batch can never carry a run past the ceiling.
* A row the model skipped or mangled stays unscored and is retried - an invented
  score would be wrong forever, and here a score decides whether we apply.
"""
from __future__ import annotations

import json
import time

from . import gates
from .config import section
from .llm import LLMConfigError, RateLimited

KEYS = ("skills", "seniority", "location", "domain", "recency", "reach")
RISK = {0: "low", 1: "medium", 2: "high"}

SYSTEM = """You screen job postings for one candidate.

CANDIDATE:
{profile}

For each job output one array, positionally:
[ref, score, skills, seniority, location, domain, recency, reach, risk, "reason"]

score 0-10 (10 = perfect fit, 0 = wrong field). The six middle numbers are 0-100.
risk is scam risk: 0 low, 1 medium, 2 high (fees or deposits, vague duties, unrealistic
pay, WhatsApp/personal-email-only contact). reason is at most 10 words.

Seniority is a hard gate, not a tiebreaker: a title containing Senior, Sr, Staff,
Principal, Lead, Manager, Head, Director or VP, or a stated requirement of more years
than the candidate has, scores at most 3 however well the skills match.
Set location at most 30 when the role is onsite somewhere they cannot work.

Reply with ONLY a JSON array of these arrays. No prose, no keys, no markdown."""

USER = """Jobs, one per line as ref|company|title|location|experience|salary|description:
{jobs}"""


def build_messages(profile: str, jobs: list[dict]) -> list[dict]:
    lines = []
    for i, j in enumerate(jobs):
        desc = " ".join((j.get("description") or "")[:300].split())
        lines.append(f"{i}|{j.get('company') or '?'}|{j['title']}|{j.get('location') or '?'}|"
                     f"{j.get('experience') or '?'}|{j.get('salary') or '?'}|{desc}")
    return [{"role": "system", "content": SYSTEM.format(profile=profile[:1800])},
            {"role": "user", "content": USER.format(jobs="\n".join(lines))}]


def clean_breakdown(raw) -> dict:
    src = [raw.get(k, 0) for k in KEYS] if isinstance(raw, dict) else list(raw or [])
    out = {}
    for k, v in zip(KEYS, src + [0] * 6):
        try:
            out[k] = max(0, min(100, int(v)))
        except (TypeError, ValueError):
            out[k] = 0
    return out


def _salvage(blob: str) -> list:
    """Parse whatever individual rows survive a malformed reply: rows nested one
    level inside an outer array, or bare rows with no outer array at all."""
    for level in (2, 1):
        out, depth, start = [], 0, -1
        for i, ch in enumerate(blob):
            if ch in "[{":
                depth += 1
                if depth == level:
                    start = i
            elif ch in "]}" and depth:
                if depth == level and start >= 0:
                    try:
                        item = json.loads(blob[start:i + 1])
                        if isinstance(item, (list, dict)) and item:
                            out.append(item)
                    except json.JSONDecodeError:
                        pass
                    start = -1
                depth -= 1
        if out:
            return out
    return []


def _row(item, jobs: list[dict]):
    if isinstance(item, dict):
        ref, score = item.get("ref", -1), item.get("score", 0)
        bd, risk, reason = item.get("breakdown"), item.get("risk", 0), item.get("reason", "")
    elif isinstance(item, list) and len(item) >= 2:
        ref, score = item[0], item[1]
        bd = item[2:8]
        risk = item[8] if len(item) > 8 else 0
        reason = item[9] if len(item) > 9 else (item[8] if len(item) == 9 and isinstance(item[8], str) else "")
        if isinstance(risk, str):
            risk = 0
    else:
        return None
    try:
        ref = int(ref)
    except (TypeError, ValueError):
        return None
    if not 0 <= ref < len(jobs):
        return None
    try:
        score = max(0, min(10, int(score)))
    except (TypeError, ValueError):
        return None
    try:
        risk = RISK.get(int(risk), "low")
    except (TypeError, ValueError):
        risk = "low"
    return jobs[ref]["id"], (score, str(reason)[:200], clean_breakdown(bd), risk)


def parse(text: str, jobs: list[dict]) -> dict[str, tuple]:
    """{job_id: (score, reason, breakdown, risk)}. Missing rows are absent."""
    open_at = text.find("[")
    if open_at == -1:
        raise ValueError(f"no JSON array in reply: {text[:160]}")
    close_at = text.rfind("]")
    blob = text[open_at:close_at + 1] if close_at > open_at else text[open_at:]
    try:
        items = json.loads(blob)
        if not isinstance(items, list):
            raise ValueError("reply was not an array")
        if items and not isinstance(items[0], (list, dict)):
            items = [items]                 # a single flat row
    except (json.JSONDecodeError, ValueError):
        items = _salvage(blob)
        if not items:
            raise ValueError(f"unparseable reply: {text[:160]}")
    out = {}
    for item in items:
        row = _row(item, jobs)
        if row:
            out[row[0]] = row[1]
    return out


class Budget:
    """Daily token ceiling, recorded in the shared database. A reserve is held
    back so form answers can still be written after a heavy scoring run."""

    def __init__(self, store, daily_limit: int, reserve: int = 40_000):
        self.store, self.daily_limit, self.reserve = store, daily_limit, reserve

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def used(self) -> int:
        row = self.store.db.execute("SELECT tokens FROM spend WHERE day = ?", (self._today(),)).fetchone()
        return (row[0] if row else 0) or 0

    def remaining(self) -> int:
        return max(0, self.daily_limit - self.reserve - self.used())

    def record(self, tokens: int) -> None:
        self.store.db.execute(
            "INSERT INTO spend (day, tokens) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET tokens = tokens + excluded.tokens",
            (self._today(), max(0, tokens)))
        self.store.db.commit()

    def can_afford(self, tokens: int) -> bool:
        return self.remaining() >= tokens

    def report(self) -> str:
        used = self.used()
        pct = 100 * used // self.daily_limit if self.daily_limit else 0
        return f"{used:,}/{self.daily_limit:,} tokens today ({pct}%)"


def score_pending(store, llm_client, profile: str, log=print) -> dict:
    """Score every unscored open job, inside the budget. Returns a summary."""
    cfg = section("llm")
    rows = store.unscored(int(cfg.get("max_to_score", 5000)))
    summary = {"scored": 0, "pending": len(rows), "stopped": None}
    if not rows or llm_client is None:
        return summary
    budget = Budget(store, int(cfg.get("daily_token_limit", 500_000)))
    size = int(cfg.get("batch_size", 25))
    per_batch = int(cfg.get("tokens_per_batch", 4000))
    log(f"scoring {len(rows)} jobs - budget {budget.report()}")

    def apply(job, result):
        score, reason, bd, risk = result
        score, reason = gates.apply_all(score, job["title"], bd, reason)
        store.save_score(job["id"], score, reason, bd, risk)
        summary["scored"] += 1

    def run(chunk) -> dict:
        before = llm_client.tokens_used
        try:
            text = llm_client.chat(build_messages(profile, chunk), "scoring",
                                   max_tokens=min(4000, 150 + 70 * len(chunk)))
            return parse(text, chunk)
        finally:
            # record real spend right away, so the next check sees the true figure
            budget.record(llm_client.tokens_used - before)

    dropped = []
    try:
        for start in range(0, len(rows), size):
            if not budget.can_afford(per_batch):
                summary["stopped"] = "budget"
                log(f"budget reached ({budget.report()}); {len(rows) - start} wait for tomorrow")
                break
            chunk = rows[start:start + size]
            try:
                got = run(chunk)
            except (RateLimited, LLMConfigError):
                raise
            except Exception as e:  # noqa: BLE001 - one unusable reply must not end the run
                log(f"batch failed ({type(e).__name__}), deferring {len(chunk)}")
                dropped.extend(chunk)
                continue
            for job in chunk:
                if job["id"] in got:
                    apply(job, got[job["id"]])
                else:
                    dropped.append(job)
            log(f"scored {summary['scored']}/{len(rows)}")
        # Models reliably drop a row or two from a long array; one retry in small
        # groups recovers most. The rest stay unscored for the next run.
        for start in range(0, len(dropped), 8):
            if not budget.can_afford(per_batch):
                break
            chunk = dropped[start:start + 8]
            try:
                got = run(chunk)
            except Exception:  # noqa: BLE001 - retry is best effort
                break
            for job in chunk:
                if job["id"] in got:
                    apply(job, got[job["id"]])
    except RateLimited as e:
        summary["stopped"] = "rate_limited"
        log(f"rate limited - {summary['scored']} scored, the rest wait ({e})")
    return summary


def score_one(store, llm_client, profile: str, job: dict) -> tuple | None:
    """Score a single job (after its full description loaded), inside the budget."""
    budget = Budget(store, int(section("llm").get("daily_token_limit", 500_000)))
    if not budget.can_afford(1500):
        return None
    before = llm_client.tokens_used
    try:
        text = llm_client.chat(build_messages(profile, [job]), "scoring", max_tokens=300)
    finally:
        budget.record(llm_client.tokens_used - before)
    return parse(text, [job]).get(job["id"])


def recap(store) -> int:
    """Re-apply the code gates to stored scores. Free: no model involved."""
    changed = 0
    for r in store.db.execute(
            "SELECT id, title, score, reason, breakdown FROM jobs WHERE score IS NOT NULL").fetchall():
        try:
            bd = json.loads(r["breakdown"]) if r["breakdown"] else {}
        except ValueError:
            bd = {}
        score, reason = gates.apply_all(r["score"], r["title"], bd, r["reason"] or "")
        if score != r["score"]:
            store.update(r["id"], score=score, reason=reason)
            changed += 1
    return changed
