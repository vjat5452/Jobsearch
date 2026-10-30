"""ATS coverage, and editing a resume PDF in place: only true keywords added,
same font and position, original file untouched."""
import tempfile
from pathlib import Path

import pymupdf

import _util  # noqa: F401 - puts the repo on sys.path

from jobflow import ats, tailor

# --- ATS matching ---------------------------------------------------------------
assert ats.mentions("Built APIs with Node.js and Postgres", "PostgreSQL"), "synonym counts"
assert ats.mentions("Built APIs with Node.js", "node"), "node = node.js"
assert not ats.mentions("Experienced in Google Sheets", "go"), "'go' is not in 'google'"
assert not ats.mentions("HTML and CSS", "ml")
assert ats.mentions("C++ and Python", "c++") and not ats.mentions("C++ and Python", "c")
a = ats.analyze("Python, Django, REST APIs", {"must": ["Python", "Docker"], "nice": ["RESTful APIs", "Kafka"]})
assert a["score"] == round(100 * (2 + 1) / 6) and a["missing"] == ["Docker", "Kafka"], a
assert ats.analyze("x", {"must": [], "nice": []})["score"] is None

# --- what may be added -------------------------------------------------------------
profile = {"skill_years": {"Python": 2, "Docker": 1, "Kubernetes": 0, "postgres": 1}}
assert tailor.allowed_keywords(["Docker", "Kafka", "Kubernetes", "PostgreSQL"], profile) == ["Docker", "PostgreSQL"], \
    "a skill at 0 years or not listed is never added"

text = "Skills: Python, Django, React\nBuilt a Django API serving 2,000 users"
kws = ["Python", "Docker", "Kafka"]
ok = tailor.check_edit("Skills: Python, Django, React", "Skills: Python, Django, React, Docker", ["Docker"], kws, text)
assert ok is None, ok
assert "kafka" in tailor.check_edit("Skills: Python, Django, React", "Skills: Python, Django, React, Kafka",
                                    ["Docker"], kws, text).lower()
assert "number" in tailor.check_edit("Built a Django API serving 2,000 users",
                                     "Built a Django API serving 20,000 users", [], kws, text)
assert "drops" in tailor.check_edit("Built a Django API serving 2,000 users", "Docker expert 2,000", ["Docker"], kws, text)
assert tailor.check_edit("Skills: Python", "Skills: Python, Kubernetes", ["Docker"], kws, text) is not None, \
    "a vocabulary skill you do not have is rejected even if the posting did not list it"

# --- in-place PDF edit ----------------------------------------------------------------
d = Path(tempfile.mkdtemp())
src = d / "My_Resume.pdf"
doc = pymupdf.open()
page = doc.new_page(width=595, height=842)
page.draw_rect(pymupdf.Rect(40, 40, 555, 70), color=(0.2, 0.3, 0.6), fill=(0.9, 0.93, 1))   # a header band
page.insert_text((50, 60), "TEST PERSON", fontname="hebo", fontsize=16)
page.insert_text((50, 100), "Skills: Python, Django, React, PostgreSQL", fontname="helv", fontsize=10.5,
                 color=(0.1, 0.1, 0.1))
page.insert_text((50, 118), "Built a Django API serving 2,000 users across three teams", fontname="tiro", fontsize=10.5)
page.draw_line((40, 130), (555, 130), color=(0.5, 0.5, 0.5))
doc.save(str(src))
doc.close()
original = src.read_bytes()

with pymupdf.open(str(src)) as doc:
    lines = tailor.editable_lines(doc)
texts = [ln["text"] for ln in lines]
assert "TEST PERSON" not in texts, "headings are not edited"
skills = next(ln for ln in lines if ln["text"].startswith("Skills"))
bullet = next(ln for ln in lines if ln["text"].startswith("Built"))

out = d / "edited.pdf"
done = tailor.apply_edits(src, out, [(skills, "Skills: Python, Django, React, PostgreSQL, Docker"),
                                     (bullet, "Built a Django REST API serving 2,000 users across three teams")])
assert len(done) == 2
assert src.read_bytes() == original, "your original file is never modified"

with pymupdf.open(str(out)) as doc:
    page = doc[0]
    assert "Docker" in page.get_text() and "REST API" in page.get_text()
    assert page.get_text().count("Skills:") == 1, "old text removed, not overlaid"
    spans = {s["text"].strip(): s for b in page.get_text("dict")["blocks"] if b["type"] == 0
             for ln in b["lines"] for s in ln["spans"]}
    new_skills = spans["Skills: Python, Django, React, PostgreSQL, Docker"]
    assert abs(new_skills["origin"][1] - skills["origin"][1]) < 0.5, "same baseline"
    assert abs(new_skills["origin"][0] - skills["origin"][0]) < 0.5, "same left edge"
    assert abs(new_skills["size"] - skills["size"]) < 0.7, "same size (at most a slight shrink to fit)"
    assert "Helvetica" in new_skills["font"] or "helv" in new_skills["font"].lower(), new_skills["font"]
    new_bullet = spans["Built a Django REST API serving 2,000 users across three teams"]
    assert "Times" in new_bullet["font"] or "tiro" in new_bullet["font"].lower(), "serif stays serif"
    assert page.get_drawings(), "the header band and rule survive"
    assert "TEST PERSON" in page.get_text(), "untouched lines stay"

# a line that cannot fit its width is left as it was
too_long = "Skills: " + ", ".join(["Python"] * 40)
assert tailor.apply_edits(src, d / "x.pdf", [(skills, too_long)]) == []
# a right-aligned date on the same line limits the room: the edit must not run into it
src2 = d / "dated.pdf"
doc = pymupdf.open()
page = doc.new_page(width=595, height=842)
page.insert_text((50, 100), "Backend Developer working on payment services", fontname="helv", fontsize=10.5)
page.insert_text((470, 100), "2023 - 2025", fontname="helv", fontsize=10.5)
page.insert_text((50, 200), "A much longer line that sets the right edge of the text column for this page here",
                 fontname="helv", fontsize=10.5)
doc.save(str(src2))
doc.close()
with pymupdf.open(str(src2)) as doc:
    role = next(ln for ln in tailor.editable_lines(doc) if ln["text"].startswith("Backend"))
assert role["origin"][0] + role["width"] <= 470 - 7, role["width"]
assert tailor.apply_edits(src2, d / "y.pdf", [(role, role["text"] + " with Django, Docker, Kubernetes, Redis and PostgreSQL in production")]) == []
print("tailor tests PASS")
