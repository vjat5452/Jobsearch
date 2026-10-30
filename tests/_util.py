"""Shared test helpers. Tests are plain scripts: python tests/test_x.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobflow.models import make_job  # noqa: E402
from jobflow.store import Store  # noqa: E402


def fresh_store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "t.db"))


def job(platform="linkedin", nid="1", title="Software Engineer", company="Acme", kind="easy", **kw):
    return make_job(platform, nid, title=title, company=company, url=f"https://x/{platform}/{nid}",
                    apply_kind=kind, **kw)


class FakeLLM:
    """Stands in for llm.LLM: returns canned replies and counts tokens."""

    def __init__(self, reply, tokens_per_call=1000):
        self.reply, self.per_call = reply, tokens_per_call
        self.tokens_used, self.calls, self.last_messages = 0, 0, None

    def chat(self, messages, purpose="scoring", max_tokens=1200):
        self.calls += 1
        self.tokens_used += self.per_call
        self.last_messages = messages
        return self.reply(messages) if callable(self.reply) else self.reply
