"""The hybrid decision, per the example settings: auto_min 7, review_min 6,
ats always reviewed, prefer ats > linkedin > naukri > indeed."""
from _util import fresh_store, job

from jobflow import router


def scored(j, score, risk="low", desc=""):
    return {**j, "score": score, "scam_risk": risk, "description": desc}


# --- decide -------------------------------------------------------------------
assert router.decide(scored(job(), 8))[:2] == ("auto", "queued"), "easy + strong = auto"
assert router.decide(scored(job(), 7))[0] == "auto", "threshold is inclusive"
r = router.decide(scored(job(), 6))
assert r[0] == "review" and "below auto" in r[2], "grey zone goes to review"
assert router.decide(scored(job(), 5))[0] == "skip"
assert router.decide(scored(job(kind="external"), 9))[0] == "review", "external never auto"
assert router.decide(scored(job("ats", kind="ats_form"), 10))[0] == "review", "company forms reviewed"
assert router.decide(scored(job(), 9, risk="medium"))[0] == "review", "medium scam risk is checked by you"
assert router.decide(scored(job(), 9, risk="high"))[0] == "skip"
assert router.decide(scored(job(), 9, desc="Pay a registration fee of 500"))[0] == "skip"
assert router.decide({**job(), "score": None})[0] == "", "unscored jobs are not routed"

# --- duplicates across sites ------------------------------------------------------
s = fresh_store()
li = job("linkedin", "1", title="Python Developer", company="Acme Technologies Pvt Ltd")
nk = job("naukri", "1", title="Python Developer (Remote)", company="ACME Technologies")
assert li["fingerprint"] == nk["fingerprint"], "same role, different decoration"
s.upsert([li, nk])
s.save_score(li["id"], 8, "", {}, "low")
s.save_score(nk["id"], 8, "", {}, "low")
counts = router.route_pending(s)
assert counts["auto"] == 1 and counts["duplicate"] == 1, counts
assert s.get(li["id"])["status"] == "queued", "linkedin is preferred over naukri"
assert s.get(nk["id"])["status"] == "duplicate"

# a copy on a more preferred site takes over a merely queued one
s2 = fresh_store()
nk2 = job("naukri", "2", title="Data Engineer", company="Beta")
s2.upsert([nk2])
s2.save_score(nk2["id"], 8, "", {}, "low")
router.route_pending(s2)
assert s2.get(nk2["id"])["status"] == "queued"
li2 = job("linkedin", "2", title="Data Engineer", company="Beta")
s2.upsert([li2])
s2.save_score(li2["id"], 8, "", {}, "low")
router.route_pending(s2)
assert s2.get(li2["id"])["status"] == "queued"
assert s2.get(nk2["id"])["status"] == "duplicate"

# ...but never one that was already applied to or that you approved
s2.update(li2["id"], status="applied")
ind = job("indeed", "2", title="Data Engineer", company="Beta")
s2.upsert([ind])
s2.save_score(ind["id"], 9, "", {}, "low")
router.route_pending(s2)
assert s2.get(ind["id"])["status"] == "duplicate"
print("router tests PASS")
