"""Edit YOUR resume for one job - same file, same fonts, same layout.

When a job is a reasonable fit but your resume's ATS keyword coverage for it is
low (below tailor.ats_min), a few existing lines are reworded so the posting's
keywords appear, and only keywords you actually have:

  1. ats.py lists the posting's keywords and which ones your resume lacks.
  2. A missing keyword may be added only if it is in profile.yaml `skill_years`.
     Anything else stays missing and is shown to you as a gap, never written.
  3. The model rewrites up to 6 existing lines (a skills line, a bullet) to work
     those keywords in. Each edit is then checked in code: no new numbers, no
     skill outside the allowed set, most of the original wording kept, and it
     must fit the line's width in the original font and size.
  4. The PDF is edited in place with PyMuPDF: the old line's text is removed and
     the new text drawn at the same position, size and colour, in the resume's
     own embedded font when it has the needed glyphs (else the closest standard
     font: serif/sans/mono, bold/italic as the original).
  5. The ATS score is re-measured; the edited copy is kept only if it improved.

Your original file is never modified - edited copies go to data/resumes/<job>/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import ats, llm
from .config import DATA_DIR, PROFILE, resume_path, section

RESUMES_DIR = DATA_DIR / "resumes"
MAX_EDITS = 6
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_CONTACT = re.compile(r"@|https?://|www\.|linkedin\.com|github\.com|\+?\d[\d\s-]{8,}\d")


def enabled() -> bool:
    return bool(section("tailor").get("enabled", True))


def ats_min() -> int:
    return int(section("tailor").get("ats_min", 70))


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text or "")}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9+#.]+", (text or "").lower()) if len(w) > 3]


# ---------------------------------------------------------------------------
# What may be added
# ---------------------------------------------------------------------------

def owned_skills(profile: dict | None = None) -> set[str]:
    profile = profile if profile is not None else PROFILE
    return {ats.canon(str(k)) for k, v in (profile.get("skill_years") or {}).items()
            if v not in (None, 0, "0", False)}


def allowed_keywords(missing: list[str], profile: dict | None = None) -> list[str]:
    owned = owned_skills(profile)
    return [k for k in missing if ats.canon(k) in owned]


def check_edit(old: str, new: str, allowed: list[str], all_keywords: list[str], resume_text: str) -> str | None:
    """Why this edit is rejected, or None if it is safe. Pure - tested directly."""
    new = (new or "").strip()
    if not new or new == old.strip():
        return "unchanged"
    if not _numbers(new) <= _numbers(old):
        return "new or changed number"
    allowed_c = {ats.canon(k) for k in allowed}
    for term in set(all_keywords) | ats.VOCAB:
        if ats.mentions(new, term) and not ats.mentions(old, term) and ats.canon(term) not in allowed_c \
                and not ats.mentions(resume_text, term):
            return f"adds '{term}', which you have not listed"
    old_w = _words(old)
    if old_w:
        kept = sum(1 for w in set(old_w) if w in new.lower())
        if kept / len(set(old_w)) < 0.6:
            return "drops too much of the original line"
    return None


# ---------------------------------------------------------------------------
# PDF: read lines, write lines
# ---------------------------------------------------------------------------

def _rgb(c: int) -> tuple[float, float, float]:
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


def editable_lines(doc) -> list[dict]:
    """Lines set in one style that are worth editing (not headings, not contact)."""
    out = []
    for pno, page in enumerate(doc):
        blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0]
        boxes = [ln["bbox"] for b in blocks for ln in b["lines"] if "".join(s["text"] for s in ln["spans"]).strip()]
        # The text column's right edge: the widest line on the page, or a symmetric
        # margin when every line is short.
        left = min((b[0] for b in boxes), default=36)
        content_right = max([b[2] for b in boxes] + [page.rect.width - left])

        def room(bbox):
            """Up to the next text on the same line (a right-aligned date, a second
            column), else the column's right edge."""
            x0, y0, x1, y1 = bbox
            limit = content_right
            for b in boxes:
                if b[0] > x1 + 1 and min(y1, b[3]) - max(y0, b[1]) > 0.4 * (y1 - y0):
                    limit = min(limit, b[0] - 8)
            return max(limit, x1)

        for block in blocks:
            for line in block["lines"]:
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                style = {(s["font"], round(s["size"], 1), s["color"]) for s in spans}
                text = "".join(s["text"] for s in line["spans"]).strip()
                if len(style) != 1 or len(text) < 15 or _CONTACT.search(text):
                    continue
                if text.isupper() and len(text) < 40:
                    continue                                # a section heading
                if line["dir"] != (1.0, 0.0):
                    continue                                # rotated text
                s0 = spans[0]
                out.append({
                    "i": len(out), "page": pno, "bbox": tuple(line["bbox"]), "text": text,
                    "font": s0["font"], "size": s0["size"], "color": s0["color"], "flags": s0["flags"],
                    "origin": tuple(s0["origin"]),
                    "width": room(line["bbox"]) - s0["origin"][0],
                })
    return out


_BASE14 = {  # (serif, mono) -> regular, bold, italic, bold-italic
    (False, False): ("helv", "hebo", "heit", "hebi"),
    (True, False): ("tiro", "tibo", "tiit", "tibi"),
    (False, True): ("cour", "cobo", "coit", "cobi"),
    (True, True): ("cour", "cobo", "coit", "cobi"),
}


def _font_for(doc, page, line: dict, text: str):
    """(font object, insert kwargs). The resume's own embedded font if it can draw
    every character; else the closest standard font."""
    import pymupdf

    want = line["font"].split("+")[-1]
    for xref, ext, _typ, basefont, *_ in page.get_fonts(full=True):
        if basefont.split("+")[-1] != want:
            continue
        try:
            _name, ext, _st, buf = doc.extract_font(xref)
        except Exception:  # noqa: BLE001
            buf = b""
        if buf and ext not in ("n/a", ""):
            try:
                font = pymupdf.Font(fontbuffer=buf)
                if all(font.has_glyph(ord(ch)) for ch in text if not ch.isspace()):
                    return font, {"fontname": f"JF{xref}", "fontbuffer": buf}
            except Exception:  # noqa: BLE001 - unusable embedded font: fall through
                pass
        break
    flags, name = line["flags"], line["font"].lower()
    bold = bool(flags & 16) or "bold" in name
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    code = _BASE14[(bool(flags & 4), bool(flags & 8))][(1 if bold else 0) + (2 if italic else 0)]
    return pymupdf.Font(code), {"fontname": code}


def _fit(font, text: str, size: float, width: float) -> float | None:
    for scale in (1.0, 0.97, 0.94):
        if font.text_length(text, fontsize=size * scale) <= width + 0.5:
            return size * scale
    return None


def apply_edits(src: Path, dst: Path, edits: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """Write `edits` [(line, new_text)] into a copy of src. Returns the edits that
    fitted; lines that would overflow are left as they were."""
    import pymupdf

    doc = pymupdf.open(str(src))
    plans = []
    for line, new in edits:
        page = doc[line["page"]]
        font, kw = _font_for(doc, page, line, new)
        size = _fit(font, new, line["size"], line["width"])
        if size is None:
            continue
        plans.append((line, new, size, kw))
    by_page: dict[int, list] = {}
    for p in plans:
        by_page.setdefault(p[0]["page"], []).append(p)
    for pno, items in by_page.items():
        page = doc[pno]
        for line, *_ in items:
            x0, y0, x1, y1 = line["bbox"]
            page.add_redact_annot(pymupdf.Rect(x0, y0 + 0.5, x1, y1 - 0.5), fill=False)
        # remove only the text under those lines: keep images, rules and backgrounds
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
        for line, new, size, kw in items:
            page.insert_text(line["origin"], new, fontsize=size, color=_rgb(line["color"]), **kw)
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(dst), garbage=3, deflate=True)
    doc.close()
    return [(line, new) for line, new, _s, _k in plans]


# ---------------------------------------------------------------------------
# The model's proposal
# ---------------------------------------------------------------------------

PROMPT = """You raise a resume's keyword match for one job by rewording a few EXISTING lines.

Rules - follow strictly:
1. Edit at most {max_edits} lines. Each edit replaces one whole line and keeps its role
   (a skills line stays a skills line, a bullet stays a bullet) and most of its words.
2. Work in these keywords, which the candidate genuinely has: {allowed}.
   Put each in the one line where it fits most truthfully - usually a skills line.
3. You may also swap a word for the posting's exact term when it means the same thing.
4. Never add any other skill, tool, number, employer, title or claim. Keep every number exactly.
5. Stay within each line's character limit shown in [max N]. Shorten wording to make room.

POSTING KEYWORDS - must: {must}; nice: {nice}

RESUME LINES:
{lines}

Reply with JSON only: {{"edits": [{{"line": <number>, "text": "<the whole new line>"}}]}}"""


def propose(lines: list[dict], allowed: list[str], keywords: dict) -> list[dict]:
    client = llm.client()
    if client is None:
        return []
    shown = []
    for ln in lines[:90]:
        used = max(1.0, ln["bbox"][2] - ln["origin"][0])
        limit = max(len(ln["text"]), int(len(ln["text"]) * ln["width"] / used))
        shown.append(f"{ln['i']}: {ln['text']} [max {limit}]")
    prompt = PROMPT.format(max_edits=MAX_EDITS, allowed=", ".join(allowed),
                           must=", ".join(keywords.get("must") or []), nice=", ".join(keywords.get("nice") or []),
                           lines="\n".join(shown))
    raw = client.complete(prompt, "answers", 1500)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        return (json.loads(m.group(0)) if m else {}).get("edits") or []
    except ValueError:
        return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:40] or "x"


def resume_for(store, job: dict, log=print) -> Path | None:
    """The resume to send for this job: an edited copy when your base resume's
    ATS coverage is low and it could be improved truthfully, else None (= send
    your base resume unchanged)."""
    if not enabled():
        return None
    base = resume_path()
    if not base.exists() or base.suffix.lower() != ".pdf":
        return None
    if job.get("resume_file"):
        path = RESUMES_DIR / job["resume_file"]
        if path.exists():
            return path
    from .resume import extract_text

    text = extract_text(base)
    a = ats.for_job(store, job, text)
    before = a["score"]
    if before is None or before >= ats_min():
        if before is not None:
            log(f"   ATS {before}% - your resume already covers this posting")
        return None
    allowed = allowed_keywords(a["missing"])
    record = {"before": before, "after": None, "edits": [], "missing": a["missing"],
              "added": [], "not_yours": [k for k in a["missing"] if k not in allowed]}
    if not allowed:
        log(f"   ATS {before}% - missing {', '.join(a['missing'][:5])}: not in your skill_years, so not added")
        store.update(job["id"], tailored=json.dumps(record))
        return None
    try:
        import pymupdf

        with pymupdf.open(str(base)) as doc:
            lines = editable_lines(doc)
        by_i = {ln["i"]: ln for ln in lines}
        all_kw = (a["keywords"].get("must") or []) + (a["keywords"].get("nice") or [])
        chosen, used = [], set()
        for e in propose(lines, allowed, a["keywords"])[: MAX_EDITS * 2]:
            try:
                ln = by_i.get(int(e.get("line")))
            except (TypeError, ValueError, AttributeError):
                continue
            if ln is None or ln["i"] in used:
                continue
            why = check_edit(ln["text"], str(e.get("text") or ""), allowed, all_kw, text)
            if why:
                log(f"   edit rejected ({why}): {ln['text'][:60]}")
                continue
            chosen.append((ln, str(e["text"]).strip()))
            used.add(ln["i"])
            if len(chosen) >= MAX_EDITS:
                break
        if not chosen:
            store.update(job["id"], tailored=json.dumps(record))
            return None
        rel = Path(_safe(job["id"])) / f"{_safe(base.stem)}_{_safe(job.get('company') or 'job')}.pdf"
        out = RESUMES_DIR / rel
        done = apply_edits(base, out, chosen)
        after = ats.analyze(extract_text(out), a["keywords"])["score"] if done else None
    except Exception as e:  # noqa: BLE001 - never block an application over this
        log(f"   (resume edit failed, sending your base resume: {type(e).__name__}: {str(e)[:100]})")
        return None
    if not done or after is None or after <= before:
        out.unlink(missing_ok=True)
        log(f"   resume edit did not raise ATS ({before}% -> {after}%), sending your base resume")
        return None
    record.update(after=after, edits=[{"old": ln["text"], "new": new} for ln, new in done],
                  added=[k for k in allowed if ats.mentions(" ".join(n for _, n in done), k)])
    store.update(job["id"], resume_file=str(rel).replace("\\", "/"), tailored=json.dumps(record), ats_after=after)
    job["resume_file"] = str(rel)
    log(f"   resume edited: ATS {before}% -> {after}% (+{', '.join(record['added'])})")
    return out
