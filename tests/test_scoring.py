"""Positional parsing, the budget ceiling, and the code gates."""
from _util import FakeLLM, fresh_store, job

from jobflow import gates, scoring
from jobflow.scoring import Budget

jobs = [{**job(nid=str(i)), "description": "d"} for i in range(3)]

# --- parsing ----------------------------------------------------------------------
out = scoring.parse('[[0,8,70,80,90,60,100,85,0,"good"],[2,3,10,20,30,40,50,60,2,"weak"]]', jobs)
assert out[jobs[0]["id"]][0] == 8 and out[jobs[0]["id"]][2]["skills"] == 70
assert out[jobs[0]["id"]][1] == "good" and out[jobs[0]["id"]][3] == "low"
assert out[jobs[2]["id"]][3] == "high"
assert jobs[1]["id"] not in out, "a skipped row stays unscored"
# a 9-column row without the risk column still parses
old = scoring.parse('[[1,6,50,50,50,50,50,50,"fine"]]', jobs)
assert old[jobs[1]["id"]][:2] == (6, "fine") and old[jobs[1]["id"]][3] == "low"
# a malformed array keeps what survives
salv = scoring.parse('noise [[0,7,1,1,1,1,1,1,0,"a"], [1,6,oops], [2,5,1,1,1,1,1,1,0,"c"]]', jobs)
assert set(salv) == {jobs[0]["id"], jobs[2]["id"]}, salv
assert scoring.parse('[0,7]', jobs)[jobs[0]["id"]][0] == 7, "single flat row"
try:
    scoring.parse("sorry, I cannot", jobs)
    raise AssertionError("prose must raise")
except ValueError:
    pass

# the cacheable prefix does not depend on the batch
a = scoring.build_messages("P", jobs)[0]["content"]
b = scoring.build_messages("P", jobs[:1])[0]["content"]
assert a == b

# --- budget: never past the ceiling, even when batches cost 5x the estimate --------
s = fresh_store()
s.upsert([job(nid=f"k{i}", title=f"Software Engineer {i}") for i in range(500)])
greedy = FakeLLM("[]", tokens_per_call=20_000)
scoring.section("llm").update({"daily_token_limit": 100_000, "tokens_per_batch": 4000, "batch_size": 25})
res = scoring.score_pending(s, greedy, "profile", log=lambda *_: None)
used = Budget(s, 100_000).used()
assert used <= 100_000, used
assert greedy.calls < 10, greedy.calls
assert res["stopped"] == "budget"
calls = greedy.calls
scoring.score_pending(s, greedy, "profile", log=lambda *_: None)
assert greedy.calls == calls, "an exhausted day sends nothing"

# --- a model that ignores the seniority rule is still capped ------------------------
s2 = fresh_store()
s2.upsert([job(nid="sen", title="Senior Staff Engineer"), job(nid="jun", title="Software Engineer Intern")])


def defiant(messages):
    n = messages[1]["content"].count("\n")
    return "[" + ",".join(f'[{i},10,90,90,90,90,90,90,0,"x"]' for i in range(n)) + "]"


scoring.section("llm").update({"daily_token_limit": 1_000_000})
scoring.score_pending(s2, FakeLLM(defiant, 100), "profile", log=lambda *_: None)
assert s2.get("linkedin:sen")["score"] == 3
assert s2.get("linkedin:jun")["score"] == 10

# --- gates on their own ---------------------------------------------------------------
assert gates.is_senior("Senior Software Engineer") and not gates.is_senior("Software Engineer Intern")
assert not gates.is_senior("Leadership Development Intern")
assert gates.is_senior("Associate Director")
assert gates.relevance_cap(8, {"skills": 30, "domain": 40})[0] == 3
# rows with no outer array still parse
bare = scoring.parse('[0,7,1,1,1,1,1,1,0,"a"]\n[1,6,1,1,1,1,1,1,0,"b"]', jobs)
assert set(bare) == {jobs[0]["id"], jobs[1]["id"]}, bare
assert gates.relevance_cap(8, {"skills": 30, "domain": 80})[0] == 8, "strong in one dimension passes"
assert gates.relevance_cap(8, {k: 0 for k in scoring.KEYS})[0] == 8, "no data is not a bad fit"
print("scoring tests PASS")
