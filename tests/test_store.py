"""The diff, closures, the answer queue, and two threads sharing the database."""
import threading
import time

from _util import fresh_store, job

s = fresh_store()

# --- new vs seen ---------------------------------------------------------------
a, b = job(nid="a"), job(nid="b", title="Backend Engineer")
assert [j["id"] for j in s.upsert([a, b])] == ["linkedin:a", "linkedin:b"]
assert s.upsert([a, b]) == [], "a job seen before is not new"
assert s.get("linkedin:a")["status"] == "new"

# a longer description replaces a card snippet, never the other way round
s.upsert([{**a, "description": "full posting text " * 20}])
assert len(s.get("linkedin:a")["description"]) > 100
s.upsert([{**a, "description": "short"}])
assert len(s.get("linkedin:a")["description"]) > 100

# --- board closures only for boards that fetched cleanly -------------------------
g1 = job("ats", "g1", company="Stripe", kind="ats_form")
g2 = job("ats", "g2", company="Notion", kind="ats_form")
s.upsert([g1, g2])
time.sleep(0.01)
s.upsert([])                                     # next run: both boards' jobs missing
assert s.mark_closed("ats", ["Stripe"]) == 1, "only the healthy board closes"
assert s.get("ats:g1")["status"] == "closed"
assert s.get("ats:g2")["closed_at"] is None, "a failed fetch must not close jobs"

# reopening: back on the board -> open again, not duplicated
assert s.upsert([g1]) == []
assert s.get("ats:g1")["closed_at"] is None and s.get("ats:g1")["status"] == "new"

# an applied job is never flipped to 'closed'
s.update("ats:g2", status="applied")
time.sleep(0.01)
s.upsert([])
s.mark_closed("ats", ["Notion"])
assert s.get("ats:g2")["status"] == "applied"

# --- questions release blocked jobs to the queue they came from ------------------
s.update("linkedin:a", status="needs_input", route="auto", note="notice period")
s.update("linkedin:b", status="needs_input", route="review", note="notice period")
s.save_question("notice period", "Notice period?", "text", [])
assert s.pending_questions()[0]["blocked"] == 2
assert s.set_answer("notice period", "15") == 2
assert s.get("linkedin:a")["status"] == "queued", "auto job returns to the auto queue"
assert s.get("linkedin:b")["status"] == "approved", "review job was approved before it blocked"
assert s.get_answer("notice period") == "15"
assert s.pending_questions() == []

# approved jobs are applied before auto ones
s.update("linkedin:a", score=9)
s.update("linkedin:b", score=6)
assert [j["id"] for j in s.jobs_to_apply("linkedin")] == ["linkedin:b", "linkedin:a"]

# --- the worker and the dashboard share the file -----------------------------------
errors = []


def writer(n):
    try:
        for i in range(20):
            s.log_event("t", f"{n}-{i}")
    except Exception as e:  # noqa: BLE001
        errors.append(e)


threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
assert not errors, errors
assert len(s.events_since(0, 500)) == 80
print("store tests PASS")
