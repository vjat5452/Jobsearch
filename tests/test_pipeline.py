"""The apply loop against a fake site: outcomes, stops, caps, dry runs, and the
answer bank. No browser, no network."""
import types

from _util import fresh_store, job

from jobflow import answers, filters, human, pipeline
from jobflow.sites import Session

pipeline.llm.client = lambda: None          # no model: never touch the network
human.between_applications = lambda log=print: None
human.pause = lambda *a, **k: None
pipeline.screenshot = lambda page, name: ""
quiet = lambda *a, **k: None  # noqa: E731


class Page:
    def close(self):
        pass


class Ctx:
    def new_page(self):
        return Page()


def fake_site(script, logged_in=True, desc="A python role. " * 40):
    """script: job_id -> (open_state, apply_result)."""
    seen = []

    def open_job(page, j):
        j["description"] = desc
        return script[j["id"]][0], None

    def apply_job(page, j, handle, sess):
        seen.append(j["id"])
        sess.qa.append({"q": "Notice period", "a": "15", "from": "saved"})
        return script[j["id"]][1]

    mod = types.SimpleNamespace(PLATFORM="linkedin", open_job=open_job, apply_job=apply_job,
                                logged_in=lambda page: logged_in)
    return mod, seen


def queued_store(ids, status="queued"):
    s = fresh_store()
    for i in ids:
        j = job(nid=i, title=f"Python Developer {i}")
        s.upsert([j])
        s.update(j["id"], score=8, route="auto", status=status)
    return s


# --- outcomes are recorded, with the answers given -------------------------------------
s = queued_store(["a", "b", "c", "d", "e"])
mod, seen = fake_site({
    "linkedin:a": ("apply", ("applied", None)),
    "linkedin:b": ("apply", ("needs_input", "expected ctc")),
    "linkedin:c": ("already", None),
    "linkedin:d": ("external", None),
    "linkedin:e": ("closed", None),
})
pipeline.sites.get = lambda name: mod
res = pipeline.apply_round(Ctx(), s, "linkedin", log=quiet)
assert res["stop"] is None and res["tally"]["applied"] == 1, res
assert s.get("linkedin:a")["status"] == "applied" and s.get("linkedin:a")["applied_at"]
assert s.get("linkedin:b")["status"] == "needs_input" and s.get("linkedin:b")["note"] == "expected ctc"
assert s.get("linkedin:c")["status"] == "applied", "already applied by hand counts"
d = s.get("linkedin:d")
assert d["status"] == "review" and d["apply_kind"] == "external", "external at open time goes to you"
assert s.get("linkedin:e")["status"] == "closed"
app = s.applications()
assert {a["result"] for a in app} == {"applied", "needs_input"}
assert "Notice period" in app[0]["qa"], "the audit trail keeps what was answered"

# --- a site limit stops the round and keeps the job for later ---------------------------
s = queued_store(["a", "b"])
s.update("linkedin:a", score=9)                 # best score is tried first
mod, seen = fake_site({"linkedin:a": ("apply", ("quota", "limit")), "linkedin:b": ("apply", ("applied", None))})
pipeline.sites.get = lambda name: mod
res = pipeline.apply_round(Ctx(), s, "linkedin", log=quiet)
assert res["stop"] == "quota" and seen == ["linkedin:a"]
assert s.get("linkedin:a")["status"] == "queued", "retried tomorrow, not lost"

# --- verification check -> stop before touching anything ----------------------------------
s = queued_store(["a"])
mod, seen = fake_site({"linkedin:a": ("blocked", None)})
pipeline.sites.get = lambda name: mod
assert pipeline.apply_round(Ctx(), s, "linkedin", log=quiet)["stop"] == "blocked" and not seen

# --- logged out -> nothing attempted -------------------------------------------------------
mod, seen = fake_site({"linkedin:a": ("apply", ("applied", None))}, logged_in=False)
pipeline.sites.get = lambda name: mod
assert pipeline.apply_round(Ctx(), s, "linkedin", log=quiet)["stop"] == "logged_out" and not seen
assert s.site("linkedin")["logged_in"] == 0

# --- three failures in a row pause the site -------------------------------------------------
s = queued_store(["a", "b", "c", "d"])
mod, seen = fake_site({f"linkedin:{x}": ("apply", ("failed", "form rejected")) for x in "abcd"})
pipeline.sites.get = lambda name: mod
res = pipeline.apply_round(Ctx(), s, "linkedin", log=quiet)
assert res["stop"] == "failures" and len(seen) == 3

# --- daily cap ------------------------------------------------------------------------------
s = queued_store(["a", "b", "c"])
mod, seen = fake_site({f"linkedin:{x}": ("apply", ("applied", None)) for x in "abc"})
pipeline.sites.get = lambda name: mod
pipeline.section("daily_caps")["linkedin"] = 2
res = pipeline.apply_round(Ctx(), s, "linkedin", log=quiet)
assert res["stop"] == "cap" and res["tally"]["applied"] == 2
pipeline.section("daily_caps")["linkedin"] = 25

# --- dry run never changes state ----------------------------------------------------------------
s = queued_store(["a"])
mod, seen = fake_site({"linkedin:a": ("apply", ("dry_run", "stopped before Submit"))})
pipeline.sites.get = lambda name: mod
pipeline.apply_round(Ctx(), s, "linkedin", dry_run=True, log=quiet)
assert s.get("linkedin:a")["status"] == "queued"
assert s.applications()[0]["result"] == "dry_run"

# --- a scam phrase found on the full posting stops an auto apply ---------------------------------
s = queued_store(["a"])
mod, seen = fake_site({"linkedin:a": ("apply", ("applied", None))},
                      desc="Great role! A refundable deposit of Rs 2000 is required.")
pipeline.sites.get = lambda name: mod
pipeline.apply_round(Ctx(), s, "linkedin", log=quiet)
assert not seen and s.get("linkedin:a")["status"] == "skipped"

# --- the answer bank ---------------------------------------------------------------------------
assert answers.match_option("yes", ["Yes", "No"]) == "Yes"
assert answers.match_option("15 days", ["Immediate", "15 Days or less", "1 month"]) == "15 Days or less"
assert answers.match_option("banana", ["Yes", "No"]) is None
assert answers.question_key("  How many years of Python?* ") == "how many years of python"
s = fresh_store()
s.save_question("notice period in days", "Notice period in days", "text", [])
s.set_answer("notice period in days", "15")
sess = Session(s, log=quiet)
assert answers.resolve(sess, "Notice period (in days)?", "text", None, {})[0] == "15"
assert sess.qa[0]["from"] == "saved"
answers.llm.answer_question = lambda *a, **k: None      # the model cannot answer
ans, key = answers.resolve(sess, "Do you hold a security clearance?", "radio", ["Yes", "No"], {})
assert ans is None and s.pending_questions()[0]["key"] == key, "unknown facts are asked, never guessed"

# --- filters --------------------------------------------------------------------------------------
assert filters.prefilter({"title": "Senior Python Developer"}) == "title excluded"
assert filters.prefilter({"title": "Telecaller"}) is not None
assert filters.prefilter({"title": "PHP Developer"}) == "different tech stack"
assert filters.prefilter({"title": "Python Developer", "experience": "5-8 Yrs"}) == "needs 5+ yrs"
assert filters.prefilter({"title": "Python Developer", "description": "pay a joining fee"}).startswith("scam")
assert filters.prefilter({"title": "Python Developer", "experience": "0-2 Yrs"}) is None
print("pipeline tests PASS")
