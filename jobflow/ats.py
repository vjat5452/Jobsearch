"""ATS match: how much of what a posting asks for appears on your resume.

Applicant-tracking systems mostly rank on keyword coverage, so this does too:
the model lists the posting's keywords once (cached per job), then coverage is
plain arithmetic - "must have" keywords count double. Synonyms count as a match
(Postgres = PostgreSQL, Node = Node.js, k8s = Kubernetes), because a recruiter's
search treats them the same and so should the edit decision.
"""
from __future__ import annotations

import json
import re

from . import llm

# A posting's keywords when no model is available: scanned for these terms.
VOCAB = {
    "python", "java", "javascript", "typescript", "go", "rust", "c++", "c#", "c", "ruby", "php", "kotlin",
    "swift", "scala", "sql", "bash", "html", "css", "sass", "tailwind",
    "react", "next.js", "angular", "vue", "svelte", "redux", "node.js", "express", "nestjs", "django",
    "flask", "fastapi", "spring", "spring boot", "rails", "laravel", ".net", "graphql", "rest", "grpc",
    "postgresql", "mysql", "mongodb", "redis", "sqlite", "dynamodb", "elasticsearch", "kafka", "rabbitmq",
    "aws", "azure", "google cloud", "docker", "kubernetes", "terraform", "jenkins", "github actions",
    "ci/cd", "linux", "nginx", "git", "microservices", "system design", "data structures", "algorithms",
    "machine learning", "deep learning", "nlp", "computer vision", "llm", "rag", "pytorch", "tensorflow",
    "pandas", "numpy", "scikit-learn", "spark", "airflow", "power bi", "tableau", "excel",
    "jest", "pytest", "cypress", "playwright", "selenium", "unit testing", "agile", "scrum", "jira",
    "android", "ios", "flutter", "react native", "figma", "websocket", "oauth", "jwt",
}

ALIASES = {
    "node.js": ["node", "nodejs", "node js"], "postgresql": ["postgres", "psql"],
    "javascript": ["js", "es6"], "typescript": ["ts"], "kubernetes": ["k8s"], "react": ["reactjs", "react.js"],
    "next.js": ["nextjs"], "vue": ["vuejs", "vue.js"], "mongodb": ["mongo"], "go": ["golang"],
    "rest": ["rest api", "rest apis", "restful", "restful api", "restful apis"],
    "machine learning": ["ml"], "ci/cd": ["ci cd", "cicd", "continuous integration"],
    "google cloud": ["gcp", "google cloud platform"], "aws": ["amazon web services"],
    "c#": ["csharp"], ".net": ["dotnet", "dot net"], "scikit-learn": ["sklearn"],
    "github actions": ["gh actions"], "llm": ["llms", "large language models", "large language model"],
}
_CANON = {a: k for k, vs in ALIASES.items() for a in vs}

PROMPT = """List the keywords an applicant-tracking system would match for this job posting:
skills, tools, languages, frameworks, platforms, methods, certifications, degrees.
Each keyword 1-3 words, as written in the posting. No soft skills ("team player",
"communication"), no company or product names, no job titles.
"must": required / core (max 15). "nice": preferred / bonus (max 10).
Reply with JSON only: {"must": ["..."], "nice": ["..."]}

POSTING:
"""


def canon(term: str) -> str:
    t = re.sub(r"\s+", " ", (term or "").strip().lower())
    return _CANON.get(t, t)


def variants(term: str) -> list[str]:
    c = canon(term)
    return [c, *ALIASES.get(c, [])]


def mentions(text: str, term: str) -> bool:
    """Word-boundary match that survives punctuation like 'Node.js' or 'C++',
    accepting any known synonym."""
    low = (text or "").lower()
    for v in variants(term):
        if re.search(rf"(?<![\w.+#]){re.escape(v)}(?![\w+#]|\.\w)", low):
            return True
    return False


def vocab_keywords(text: str) -> dict:
    found = sorted(k for k in VOCAB if mentions(text, k))
    return {"must": found[:15], "nice": []}


def extract_keywords(job: dict) -> dict:
    """The posting's keywords - from the model when one is configured."""
    text = f"{job.get('title') or ''}\n{job.get('description') or ''}"
    try:
        client = llm.client()
    except llm.LLMConfigError:
        client = None
    if client is None or len(job.get("description") or "") < 200:
        return vocab_keywords(text)
    try:
        raw = client.complete(PROMPT + text[:6000], "scoring", 600)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {}
    except (llm.RateLimited, llm.LLMConfigError):
        return vocab_keywords(text)
    except Exception:  # noqa: BLE001 - unparseable reply: fall back to the vocabulary
        return vocab_keywords(text)

    def clean(xs):
        out, seen = [], set()
        for x in xs or []:
            x = re.sub(r"\s+", " ", str(x)).strip(" .,;")
            if 1 < len(x) <= 40 and canon(x) not in seen:
                out.append(x)
                seen.add(canon(x))
        return out
    must = clean(data.get("must"))[:15]
    nice = [k for k in clean(data.get("nice")) if canon(k) not in {canon(m) for m in must}][:10]
    return {"must": must, "nice": nice} if must or nice else vocab_keywords(text)


def analyze(resume_text: str, keywords: dict) -> dict:
    """{score 0-100 or None, matched, missing}. Must-haves weigh double."""
    must, nice = keywords.get("must") or [], keywords.get("nice") or []
    total = 2 * len(must) + len(nice)
    if not total:
        return {"score": None, "matched": [], "missing": []}
    matched, missing, got = [], [], 0
    for kw, w in [(k, 2) for k in must] + [(k, 1) for k in nice]:
        if mentions(resume_text, kw):
            matched.append(kw)
            got += w
        else:
            missing.append(kw)
    return {"score": round(100 * got / total), "matched": matched, "missing": missing}


def for_job(store, job: dict, resume_text: str) -> dict:
    """Keywords (cached on the job) and your resume's coverage of them."""
    kw = None
    if job.get("ats_keywords"):
        try:
            kw = json.loads(job["ats_keywords"])
        except ValueError:
            kw = None
    if kw is None:
        kw = extract_keywords(job)
        store.update(job["id"], ats_keywords=json.dumps(kw))
        job["ats_keywords"] = json.dumps(kw)
    result = analyze(resume_text, kw)
    store.update(job["id"], ats_score=result["score"])
    job["ats_score"] = result["score"]
    return {**result, "keywords": kw}
