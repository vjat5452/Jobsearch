"""Resolve an application question: your saved answers first, then the model
(profile facts only), else set the job aside until you answer it once."""
from __future__ import annotations

import difflib
import re

from . import llm


def question_key(question: str) -> str:
    q = re.sub(r"[^a-z0-9 ]+", " ", (question or "").lower())
    return re.sub(r"\s+", " ", q).strip()[:200]


def match_option(answer: str, options: list[str] | None) -> str | None:
    """Map a free answer onto one offered option: exact, contained, or closest."""
    if not options:
        return answer
    low = {o.lower(): o for o in options}
    a = (answer or "").strip().lower()
    if a in low:
        return low[a]
    for o in options:
        if a and (a in o.lower() or o.lower() in a):
            return o
    close = difflib.get_close_matches(a, list(low), n=1, cutoff=0.5)
    return low[close[0]] if close else None


def resolve(sess, question: str, kind: str, options, job: dict, hint: str = "",
            error: str = "") -> tuple[str | None, str]:
    """Returns (answer, key). answer is None when you must answer it first.
    hint  = the form's helper text ("Example: 200000") so the format is right
    error = the site's validation message when a previous answer was rejected"""
    store = sess.store
    key = question_key(question)
    stored = store.get_answer(key)
    if stored is not None and not error:
        ans = match_option(stored, options) if options and kind != "checkbox" else stored
        if ans:
            sess.qa.append({"q": question, "a": ans, "from": "saved"})
            return ans, key

    data = llm.answer_question(question, kind, options, job, hint=hint, error=error,
                               rejected=stored if error else None)
    if data:
        ans = str(data["answer"])
        if options and kind != "checkbox":
            ans = match_option(ans, options)
        if ans:
            if data.get("reusable"):
                store.save_question(key, question, kind, options)
                store.set_answer(key, ans)
            sess.qa.append({"q": question, "a": ans, "from": "model"})
            return ans, key

    store.save_question(key, question, kind, options)
    return None, key
