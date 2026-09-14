"""Find out what each assessment is worth, by reading the unit's own documents.

Moodle's gradebook does not carry weights at Monash, but the unit overview
usually spells the whole scheme out on one slide. A real example, from the
week 1 overview of a unit whose gradebook says nothing at all:

    Individual in-class quiz - 10% (2.5% * 4): Week 3, 6, 8, 10
    Individual Assignment 1 - 20%
    Group Assignment 2 - 30%
    Individual Assignment 3 & Presentation - 40%

That is the entire scheme, including a quiz block of four when Moodle has only
ever created three of them. No amount of scraping the gradebook finds the
fourth one; reading the slide does.

Documents are read in order of how likely they are to hold the scheme, and
reading stops as soon as the weights add to 100 - there is no reason to open
thirty lecture PDFs once the answer is complete.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

try:  # optional: the packaged build ships it, a source checkout may not
    import pymupdf  # type: ignore
except ImportError:  # pragma: no cover - depends on the install
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:
        pymupdf = None  # type: ignore

from .downloader import demojibake

MAX_DOCS = 14        # a semester folder can hold a hundred PDFs
MAX_PAGES = 40       # unit guides are long; the scheme is never on page 60
COMPLETE = 0.5       # how close to 100% counts as a finished scheme

_DASH = "\u2012-\u2015\u2212\u2043-"

# "Individual Assignment 1 - 20%"  /  "Quiz - 10% (2.5% * 4)"
_DASHED = re.compile(
    r"(?P<name>[A-Za-z][^\n]{2,79}?)\s*[" + _DASH + r"]\s*"
    r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%"
    r"(?:\s*\(\s*(?P<each>\d{1,3}(?:\.\d+)?)\s*%\s*[*x×]\s*(?P<n>\d{1,2})\s*\))?")

# "ASSIGNMENT 1: INDIVIDUAL ASSIGNMENT (20%)"
_BRACKETED = re.compile(
    r"(?P<name>[A-Za-z][^\n(]{2,79}?)\s*\(\s*(?P<pct>\d{1,3}(?:\.\d+)?)\s*%\s*\)")

# Lines that mention a percentage but describe a rule, not an assessment.
_NOT_A_WEIGHT = re.compile(
    r"\b(penalt|deduct|late|similarit|plagiar|attendance rate|pass mark|"
    r"at least|minimum of|more than|less than|up to)\b", re.I)

# Names that are really section headings.
_JUNK_NAME = re.compile(
    r"^(the|this|your|a|an|it|and|or|of|in|for|to|is|are|will|you)\b", re.I)

# Grade bands out of a marking rubric. A rubric row reading "Pass (50%)" is
# describing what a mark means, not what a piece of work is worth, and it was
# the single loudest false positive when this was first run for real.
_BAND_NAME = re.compile(
    r"^(high\s+distinction|distinction|credit|pass|fail|hd|d|c|p|n|"
    r"not?\s+satisfactory|satisfactory|excellent|good|poor|average|"
    r"marks?|grade|total|weighting?|criteri[ao]n?)\b", re.I)

_PRIORITY = (
    re.compile(r"unit\s*(overview|guide|information|outline)", re.I),
    re.compile(r"assessment|marking|rubric|instruction", re.I),
    re.compile(r"week\s*0*[01]\b|introduction|orientation", re.I),
)


@dataclasses.dataclass
class Entry:
    """One weight found in a document, with the sentence that said so."""

    name: str
    weight: float
    each: float | None = None   # per-part weight, from "10% (2.5% * 4)"
    count: int | None = None    # how many parts, from the same
    source: str = ""            # "Week 01/Unit Overview.pdf p8"
    quote: str = ""

    @property
    def is_block(self) -> bool:
        return self.count is not None and self.count > 1


def read(unit_dir: Path, limit: int = MAX_DOCS) -> list[Entry]:
    """Work out a unit's marking scheme from its own documents.

    Entries are kept per document rather than pooled, because pooling is what
    goes wrong in practice: an assignment brief breaks its own 15% into tasks
    worth 6% and 3%, and adding those to the unit overview's list produced a
    unit worth 118%. A scheme is only believable if one document states the
    whole of it, so the document whose own numbers reach 100% wins outright.
    """
    best: list[Entry] = []
    for doc in candidates(unit_dir)[:limit]:
        entries = _dedupe(scan(doc, unit_dir))
        if not entries:
            continue
        if _complete(entries):
            return entries
        if sum(e.weight for e in entries) > sum(e.weight for e in best):
            best = entries
    return best


def candidates(unit_dir: Path) -> list[Path]:
    """The unit's documents, most likely to hold the scheme first.

    Transcripts come last on purpose: a lecturer saying "about twenty percent"
    is worth having when nothing else exists, but it should never outrank a
    slide that states the number.
    """
    if not unit_dir.exists():
        return []
    docs = [p for p in unit_dir.rglob("*")
            if p.suffix.lower() in (".pdf", ".docx") and not p.name.startswith("~$")]

    def rank(p: Path) -> tuple[int, int, str]:
        rel = str(p.relative_to(unit_dir)).lower()
        if "transcript" in rel or "summary" in rel:
            return (9, 0, rel)
        for i, pat in enumerate(_PRIORITY):
            if pat.search(rel):
                return (i, 0 if p.suffix.lower() == ".pdf" else 1, rel)
        return (5, 0, rel)

    return sorted(docs, key=rank)


def scan(doc: Path, root: Path | None = None) -> list[Entry]:
    """Pull every weight statement out of one document."""
    try:
        pages = _pages(doc)
    except Exception:  # noqa: BLE001 - a broken PDF must not stop a sync
        return []
    label = str(doc.relative_to(root)) if root else doc.name
    out: list[Entry] = []
    for n, text in enumerate(pages, 1):
        for line in text.splitlines():
            line = demojibake(line).strip()
            if len(line) < 5 or len(line) > 200 or "%" not in line:
                continue
            if _NOT_A_WEIGHT.search(line):
                continue
            for entry in _from_line(line):
                entry.source = f"{label} p{n}"
                out.append(entry)
    return out


def _from_line(line: str) -> list[Entry]:
    out: list[Entry] = []
    for m in _DASHED.finditer(line):
        e = _entry(m)
        if e:
            e.quote = line
            out.append(e)
    if out:
        return out
    for m in _BRACKETED.finditer(line):
        e = _entry(m)
        if e:
            e.quote = line
            out.append(e)
    return out


def _entry(m: re.Match) -> Entry | None:
    name = _tidy(m.group("name"))
    if not name:
        return None
    try:
        pct = float(m.group("pct"))
    except (TypeError, ValueError):
        return None
    if not 0 < pct <= 100:
        return None
    each = count = None
    if m.groupdict().get("each"):
        try:
            each = float(m.group("each"))
            count = int(m.group("n"))
        except (TypeError, ValueError):
            each = count = None
    return Entry(name=name, weight=pct, each=each, count=count)


def _tidy(name: str) -> str:
    name = re.sub(r"^[\s•\-*\d.)(]+", "", name).strip(" :-–—")
    name = re.sub(r"\s{2,}", " ", name)
    if len(name) < 3 or _JUNK_NAME.match(name) or _BAND_NAME.match(name):
        return ""
    if not re.search(r"[A-Za-z]{3}", name):
        return ""
    return name


def _complete(entries: list[Entry]) -> bool:
    total = sum(e.weight for e in _dedupe(entries))
    return abs(total - 100.0) <= COMPLETE


def _dedupe(entries: list[Entry]) -> list[Entry]:
    """One entry per assessment; the first document to say it wins."""
    seen: dict[str, Entry] = {}
    for e in entries:
        key = re.sub(r"[^a-z0-9]", "", e.name.lower())
        if key not in seen:
            seen[key] = e
        elif seen[key].count is None and e.count is not None:
            seen[key] = e  # the fuller statement of the same thing
    return list(seen.values())


def _pages(doc: Path) -> list[str]:
    if doc.suffix.lower() == ".docx":
        from docx import Document  # imported lazily; startup is slow enough
        d = Document(str(doc))
        body = "\n".join(p.text for p in d.paragraphs)
        for table in d.tables:
            for row in table.rows:
                body += "\n" + " - ".join(c.text.strip() for c in row.cells)
        return [body]
    if pymupdf is None:
        return []
    with pymupdf.open(str(doc)) as pdf:
        return [pdf[i].get_text() for i in range(min(pdf.page_count, MAX_PAGES))]


def available() -> bool:
    """Whether PDFs can be read at all in this install."""
    return pymupdf is not None
