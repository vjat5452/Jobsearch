"""Model calls: one client for any OpenAI-compatible endpoint (Groq by default),
plus Anthropic. Used for batch scoring (scoring.py), truthful form answers,
cover notes, and drafting profile.yaml from a resume."""
from __future__ import annotations

import json
import os
import re
import threading
import time

import requests

from .config import profile_text, section

ENDPOINTS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "xkiro": ("https://api.xkiro.com/v1", "XKIRO_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
    "anthropic": (None, "ANTHROPIC_API_KEY"),
}


class RateLimited(Exception):
    """The provider throttled us past our retries. The caller stops scoring and
    keeps what it has; unscored jobs wait for the next run."""


class LLMConfigError(RuntimeError):
    """Bad key or model name - fail loudly rather than score everything zero."""


class LLM:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg if cfg is not None else section("llm")
        self.provider = cfg.get("provider", "groq")
        if self.provider not in ENDPOINTS:
            raise LLMConfigError(f"unknown llm.provider '{self.provider}'")
        base, env = ENDPOINTS[self.provider]
        self.base_url = (cfg.get("base_url") or base or "").rstrip("/")
        env = cfg.get("key_env") or env
        self.key = os.environ.get(env, "").strip() if env else ""
        if env and not self.key:
            raise LLMConfigError(f"set {env} (env var or a line {env}=... in secrets.txt)")
        self.models = cfg.get("models") or {}
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()

    @property
    def tokens_used(self) -> int:
        return self.input_tokens + self.output_tokens

    def model(self, purpose: str) -> str:
        return self.models.get(purpose) or self.models.get("scoring") or ""

    # -- transport ---------------------------------------------------------------
    def chat(self, messages: list[dict], purpose: str = "scoring", max_tokens: int = 1200,
             retries: int = 4) -> str:
        model = self.model(purpose)
        if self.provider == "anthropic":
            return self._anthropic(messages, model, max_tokens)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        body = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
        if "gpt-oss" in model:
            # Reasoning models spend tokens thinking before they answer; give them
            # room, but keep the thinking short - this is judgement, not research.
            body["reasoning_effort"] = "low"
            body["max_tokens"] = max_tokens + 1500
        for attempt in range(retries):
            r = requests.post(f"{self.base_url}/chat/completions", headers=headers, json=body, timeout=90)
            if r.status_code == 429:
                wait = r.headers.get("retry-after")
                try:
                    delay = float(wait) if wait else 8 * (attempt + 1)
                except ValueError:
                    delay = 8 * (attempt + 1)
                time.sleep(min(delay, 60))
                continue
            if r.status_code in (401, 403):
                raise LLMConfigError(f"{self.provider} rejected the API key: {r.text[:200]}")
            if r.status_code == 404:
                raise LLMConfigError(f"model '{model}' not found on {self.provider}: {r.text[:200]}")
            if r.status_code >= 400:
                raise RuntimeError(f"{self.provider} HTTP {r.status_code}: {r.text[:200]}")
            self._pace(r.headers)
            payload = r.json()
            usage = payload.get("usage") or {}
            with self._lock:
                self.input_tokens += usage.get("prompt_tokens", 0) or 0
                self.output_tokens += usage.get("completion_tokens", 0) or 0
            text = payload["choices"][0]["message"].get("content") or ""
            return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        raise RateLimited(f"{self.provider} rate-limited after {retries} retries")

    def _pace(self, headers) -> None:
        """When the provider says the token window is nearly spent, wait for it to
        reset rather than walk into a 429."""
        try:
            remaining = int(headers.get("x-ratelimit-remaining-tokens", "999999"))
        except (TypeError, ValueError):
            return
        if remaining > 1500:
            return
        m = re.match(r"(?:(\d+)m)?([\d.]+)s", headers.get("x-ratelimit-reset-tokens", "") or "")
        secs = float(m.group(1) or 0) * 60 + float(m.group(2)) if m else 5.0
        time.sleep(min(max(secs, 1.0), 65))

    def _anthropic(self, messages: list[dict], model: str, max_tokens: int) -> str:
        import anthropic

        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        rest = [m for m in messages if m["role"] != "system"]
        client = anthropic.Anthropic(api_key=self.key)
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=rest)
        except anthropic.RateLimitError as e:
            raise RateLimited(str(e)) from e
        except anthropic.AuthenticationError as e:
            raise LLMConfigError(str(e)) from e
        with self._lock:
            self.input_tokens += resp.usage.input_tokens
            self.output_tokens += resp.usage.output_tokens
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def complete(self, prompt: str, purpose: str = "answers", max_tokens: int = 600,
                 system: str = "") -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
        return self.chat(msgs, purpose, max_tokens)


_client: LLM | None = None


def client() -> LLM | None:
    """The shared client, or None when llm.provider is 'none'."""
    global _client
    if section("llm").get("provider", "groq") in (None, "", "none"):
        return None
    if _client is None:
        _client = LLM()
    return _client


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _job_block(job: dict) -> str:
    return (f"Title: {job.get('title')}\nCompany: {job.get('company')}\n"
            f"Location: {job.get('location')}\nExperience: {job.get('experience') or '-'}\n"
            f"Salary: {job.get('salary') or '-'}\n\nDescription:\n{(job.get('description') or '')[:6000]}")


# ---------------------------------------------------------------------------
# Form answers. The bot speaks for you here, so the rules are strict.
# ---------------------------------------------------------------------------

ANSWER_SYSTEM = """You fill job-application questions on behalf of a candidate. Candidate profile (YAML):

{profile}

Rules - follow strictly:
1. Use ONLY facts in the profile. Never invent experience, numbers, certificates, visas or personal details.
2. "Years of experience in <skill>": use skill_years. If the skill is not listed, the honest answer is 0 (or "No").
3. If the answer depends on a fact that is null or missing in the profile, set "confident": false.
4. Salary: India roles -> expected_ctc_india_lpa (as LPA, or rupees if the field wants a number:
   10 LPA = 1000000). Abroad -> expected_salary_abroad. Current CTC -> current_ctc_lpa.
5. If options are given, "answer" MUST be copied exactly from the options (checkboxes: join with " | ").
6. Free-text questions (why this company, describe a project): 2-4 sentences, first person, specific,
   drawn from the profile highlights, no filler.
7. Number fields (type number, or a hint/example that is a plain number): digits only - no units or commas.
   Years as a whole number -> round down (1.5 -> 1) unless decimals are allowed.
8. "reusable": true only if the same answer is right for any company (personal facts, notice period,
   skill years, relocation). false for company- or role-specific answers.

Reply with JSON only: {{"answer": "...", "confident": true|false, "reusable": true|false}}"""


def answer_question(question: str, kind: str, options, job: dict, hint: str = "",
                    error: str = "", rejected: str | None = None) -> dict | None:
    """Returns {"answer", "reusable"} or None when the profile cannot answer it."""
    c = client()
    if c is None:
        return None
    user = (f"Job: {job.get('title')} at {job.get('company')} ({job.get('location')})\n\n"
            f"Question: {question}\nField type: {kind}\n"
            + (f"Options: {json.dumps(options)}\n" if options else "")
            + (f"Form hint (follow its format exactly): {hint}\n" if hint else "")
            + (f"The site REJECTED the previous answer {rejected!r} with: {error}\n"
               f"Give a corrected answer in the required format.\n" if error else ""))
    try:
        data = _json(c.complete(user, "answers", 500, system=ANSWER_SYSTEM.format(profile=profile_text())))
    except (LLMConfigError, RateLimited):
        raise
    except Exception as e:  # noqa: BLE001 - one bad reply sets this question aside, nothing more
        print(f"   [llm] answer error: {type(e).__name__}: {str(e)[:120]}")
        return None
    if not data.get("confident") or data.get("answer") in (None, ""):
        return None
    return data


COVER_SYSTEM = """Write a short, genuine cover note (90-140 words, plain text, no placeholders,
no "Dear Hiring Manager" cliches) for this candidate and job, using only facts from their profile:

{profile}"""


def cover_note(job: dict) -> str:
    c = client()
    if c is None:
        return ""
    try:
        return c.complete(_job_block(job), "answers", 400, system=COVER_SYSTEM.format(profile=profile_text()))
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Resume -> profile
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """Summarize this candidate into a hiring profile of at most 180 words.

START with one sentence stating, explicitly:
  - total years of PAID professional experience (internships count as partial, say so;
    coursework and personal projects are not experience)
  - the seniority band this supports: intern / new-grad / junior / mid / senior
  - whether the person is still studying, and their graduation year
Then cover primary languages and frameworks, domains, location and work authorization.
Be accurate about level even when the projects are impressive.
Plain prose, no headings.

CANDIDATE:
{text}"""


def summarize_candidate(text: str) -> str:
    c = client()
    if c is None:
        return text[:2500]
    return c.complete(SUMMARY_PROMPT.format(text=text[:12000]), "answers", 400)


DRAFT_PROMPT = """Fill this YAML template with facts from the resume below.

Rules:
- Only facts that the resume states. Anything not stated stays null.
- For a value you inferred rather than read (e.g. total_years computed from dates,
  skill_years estimated from where a skill appears), append the comment  # CONFIRM
- skill_years: one entry per concrete skill/tool, years of hands-on use, estimated from
  the dated roles and projects where it appears. Mark every one # CONFIRM.
- Keep every key of the template, same structure, same order.
Reply with the YAML only, no markdown fence.

TEMPLATE:
{template}

RESUME:
{text}"""


def draft_profile(resume_text: str, template: str) -> str:
    c = client()
    if c is None:
        raise LLMConfigError("drafting a profile needs a model - set llm.provider")
    out = c.complete(DRAFT_PROMPT.format(template=template, text=resume_text[:12000]), "answers", 2500)
    return re.sub(r"^```(?:yaml)?\s*|\s*```$", "", out.strip())
