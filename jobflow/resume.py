"""Resume text, and the short candidate summary every scoring batch reuses."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import DATA_DIR, profile_text, resume_path

SUMMARY_CACHE = DATA_DIR / ".candidate_summary.json"


def extract_text(path: str | Path | None = None) -> str:
    p = Path(path) if path else resume_path()
    if not p.exists():
        raise FileNotFoundError(f"resume not found: {p} (set resume_path in config/settings.yaml)")
    if p.suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(str(p)) as doc:
            return "\n".join(page.get_text() for page in doc)
    return p.read_text(encoding="utf-8", errors="replace")


def candidate_summary(force: bool = False) -> str:
    """One model call, cached until the resume or profile.yaml changes. A summary
    the model failed to write is not cached, so the next run tries again."""
    from . import llm

    try:
        text = extract_text()
    except FileNotFoundError:
        text = ""
    source = f"PROFILE (verified facts):\n{profile_text()}\n\nRESUME:\n{text}"
    key = hashlib.sha1(source.encode("utf-8", "ignore")).hexdigest()
    if SUMMARY_CACHE.exists() and not force:
        try:
            cached = json.loads(SUMMARY_CACHE.read_text(encoding="utf-8"))
            if cached.get("key") == key:
                return cached["summary"]
        except (ValueError, KeyError):
            pass
    try:
        summary = llm.summarize_candidate(source)
    except Exception:  # noqa: BLE001 - provider down: score from the raw facts, do not cache
        return source[:2500]
    SUMMARY_CACHE.write_text(json.dumps({"key": key, "summary": summary}), encoding="utf-8")
    return summary
