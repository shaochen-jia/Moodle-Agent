"""What you have scored so far, and what is left to score.

Moodle will not tell you this. The user report shows a mark and a range and
nothing else - no weight column, no contribution column, no course total, for
any of the four units checked. So the arithmetic has to be rebuilt here from
three sources that each know part of it:

    the gradebook   what you scored, and out of what
    the unit PDFs   what each piece is worth (see assessplan.py)
    you             everything neither of them says

That last one is not a fallback, it is half the design. A unit can be worth
"10% (2.5% * 4)" across four quizzes when Moodle has only created three of
them; a unit can state no weights anywhere. Guessing in those cases produces a
number that looks right and is wrong all semester, so every weight carries the
source it came from and anything the user edits is never overwritten again.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from pathlib import Path

# Monash grade bands, as percentages of the unit.
BANDS: tuple[tuple[str, float], ...] = (
    ("HD", 80.0), ("D", 70.0), ("C", 60.0), ("P", 50.0),
)

KINDS = ("assignment", "quiz", "test", "exam", "other")


def _round(x: float) -> float:
    """Keep stored numbers readable; floats from division are not."""
    return round(x + 0.0, 4)


@dataclasses.dataclass
class Part:
    """One piece inside a grouped item - a single quiz of a quiz block."""

    name: str
    score: float | None = None
    out_of: float = 100.0
    edited: bool = False   # typed in by hand; a sync must not overwrite it

    @property
    def fraction(self) -> float | None:
        if self.score is None or not self.out_of:
            return None
        return self.score / self.out_of


@dataclasses.dataclass
class Item:
    """One assessment: an assignment, a quiz block, a test, an exam.

    `weight` is a percentage of the whole unit. `score` is what you got out of
    `out_of` - which is the range Moodle reports, and is the thing that makes
    a bare "21" readable: 21 out of 25 is not 21% of anything until you divide.
    """

    id: str
    name: str
    kind: str = "assignment"
    weight: float | None = None
    weight_from: str = ""          # "pdf: ... p8" | "item name" | "range" | "you"
    score: float | None = None
    out_of: float = 100.0
    score_from: str = ""           # "moodle" | "you"
    parts: list[Part] = dataclasses.field(default_factory=list)
    planned: int | None = None     # how many parts there will eventually be
    count_best: int | None = None  # how many of them actually count
    include: bool = True
    source: str = "moodle"         # "moodle" | "you"
    edited: list[str] = dataclasses.field(default_factory=list)
    note: str = ""

    # -- shape ----------------------------------------------------------

    @property
    def grouped(self) -> bool:
        return bool(self.parts)

    @property
    def total_parts(self) -> int:
        return max(self.planned or 0, len(self.parts)) or 1

    @property
    def counted_parts(self) -> int:
        """How many parts are allowed to count. `best 8 of 10` makes this 8."""
        return min(self.count_best or self.total_parts, self.total_parts)

    def locked(self, field: str) -> bool:
        return field in self.edited

    def lock(self, field: str) -> None:
        if field not in self.edited:
            self.edited.append(field)

    # -- arithmetic -----------------------------------------------------

    def earned(self) -> float:
        """Percentage points of the unit already banked by this item."""
        if not self.include or self.weight is None:
            return 0.0
        if self.grouped:
            each = self.weight / self.counted_parts
            return _round(sum(f for f in self._best_fractions()) * each)
        if self.score is None or not self.out_of:
            return 0.0
        return _round(self.score / self.out_of * self.weight)

    def assessed(self) -> float:
        """Percentage points that have actually been marked.

        For a quiz block only the quizzes that came back count; the rest of the
        block is still ahead of you, and folding it in would read as though you
        had scored zero on work nobody has set yet.
        """
        if not self.include or self.weight is None:
            return 0.0
        if self.grouped:
            each = self.weight / self.counted_parts
            return _round(len(self._best_fractions()) * each)
        return self.weight if self.score is not None else 0.0

    def _best_fractions(self) -> list[float]:
        """The graded fractions that count, best first.

        Mid-semester the best 8 of 10 cannot be known, so the best of whatever
        has been graded is used and no more than `counted_parts` of them.
        """
        got = [f for f in (p.fraction for p in self.parts) if f is not None]
        got.sort(reverse=True)
        return got[: self.counted_parts]

    def graded(self) -> bool:
        return bool(self._best_fractions()) if self.grouped else self.score is not None

    def percent(self) -> float | None:
        """How well this item went, 0-100, or None if nothing is marked."""
        a = self.assessed()
        if not a:
            return None
        return _round(self.earned() / a * 100)


@dataclasses.dataclass
class Unit:
    code: str
    items: list[Item] = dataclasses.field(default_factory=list)
    target: str = ""               # "" | HD | D | C | P | a number as text
    checked_at: float = 0.0

    # -- arithmetic -----------------------------------------------------

    def live(self) -> list[Item]:
        return [i for i in self.items if i.include]

    def weight_total(self) -> float:
        return _round(sum(i.weight or 0.0 for i in self.live()))

    def weights_complete(self) -> bool:
        return abs(self.weight_total() - 100.0) < 0.05

    def unweighted(self) -> list[Item]:
        return [i for i in self.live() if i.weight is None]

    def earned(self) -> float:
        return _round(sum(i.earned() for i in self.live()))

    def assessed(self) -> float:
        return _round(sum(i.assessed() for i in self.live()))

    def remaining(self) -> float:
        """Weight still to be marked. Never negative, even if weights are off."""
        return _round(max(0.0, self.weight_total() - self.assessed()))

    def standing(self) -> float | None:
        """Your average over what has been marked, 0-100."""
        a = self.assessed()
        return _round(self.earned() / a * 100) if a else None

    def target_mark(self) -> float | None:
        """The target as a number, whether it was a band or typed in."""
        if not self.target:
            return None
        for name, cut in BANDS:
            if self.target.upper() == name:
                return cut
        try:
            return float(self.target)
        except ValueError:
            return None

    def needed(self) -> float | None:
        """Average needed across everything left, to hit the target.

        Above 100 means it is already out of reach; at or below 0 means it is
        already secured. Both are worth saying out loud rather than clamping.
        """
        want = self.target_mark()
        if want is None:
            return None
        left = self.remaining()
        if left <= 0:
            return None
        return _round((want - self.earned()) / left * 100)

    def band(self) -> str:
        """The band your current average sits in."""
        s = self.standing()
        if s is None:
            return ""
        for name, cut in BANDS:
            if s >= cut:
                return name
        return "N"


class Book:
    """Every unit's grade sheet, stored beside the app's other settings.

    Kept out of the course folder on purpose: that folder is in OneDrive, and
    two machines writing one JSON file through a file syncer is how you lose an
    afternoon of edits to a conflicted copy.
    """

    FILE = "grades.json"
    VERSION = 1

    def __init__(self, path: Path):
        self.path = path
        self.units: dict[str, Unit] = {}
        self.load()

    # -- storage --------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # a damaged sheet is re-derived; never crash the app over it
        for code, u in (raw.get("units") or {}).items():
            if not isinstance(u, dict):
                continue
            items = []
            for d in u.get("items") or []:
                if not isinstance(d, dict) or not d.get("id"):
                    continue
                parts = [Part(name=str(p.get("name", "")),
                              score=_num(p.get("score")),
                              out_of=_num(p.get("out_of")) or 100.0,
                              edited=bool(p.get("edited", False)))
                         for p in d.get("parts") or [] if isinstance(p, dict)]
                items.append(Item(
                    id=str(d["id"]), name=str(d.get("name", "")),
                    kind=str(d.get("kind", "assignment")),
                    weight=_num(d.get("weight")),
                    weight_from=str(d.get("weight_from", "")),
                    score=_num(d.get("score")),
                    out_of=_num(d.get("out_of")) or 100.0,
                    score_from=str(d.get("score_from", "")),
                    parts=parts,
                    planned=_int(d.get("planned")),
                    count_best=_int(d.get("count_best")),
                    include=bool(d.get("include", True)),
                    source=str(d.get("source", "moodle")),
                    edited=[str(x) for x in d.get("edited") or []],
                    note=str(d.get("note", "")),
                ))
            self.units[code] = Unit(code=code, items=items,
                                    target=str(u.get("target", "")),
                                    checked_at=float(u.get("checked_at", 0) or 0))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out = {"version": self.VERSION, "saved_at": time.time(), "units": {}}
        for code, unit in self.units.items():
            out["units"][code] = {
                "target": unit.target,
                "checked_at": unit.checked_at,
                "items": [dataclasses.asdict(i) for i in unit.items],
            }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)

    # -- access ---------------------------------------------------------

    def unit(self, code: str) -> Unit:
        return self.units.setdefault(code, Unit(code=code))


def merge(unit: Unit, rows: list, plan: list) -> list[str]:
    """Fold a fresh gradebook read and document scan into the sheet.

    Returns a line per mark that arrived since last time, for the desktop
    notification and the week note. Anything the user has edited is left
    alone: their number is the answer, not a stale cache of Moodle's.

    `rows` are gradebook.RawItem, `plan` are assessplan.Entry. They are taken
    structurally rather than imported, so this module stays free of anything
    that touches the network.
    """
    news: list[str] = []
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r.category, []).append(r)

    # A plan entry that says "10% (2.5% * 4)" describes a block. Match it to a
    # gradebook category so its parts are the real quizzes, and pad it out to
    # the promised count - the fourth quiz is the one Moodle never mentions.
    blocked: set[str] = set()
    for entry in [e for e in plan if getattr(e, "count", None)]:
        cat = _closest(entry.name, [c for c in by_cat if c])
        rows_in = by_cat.get(cat, []) if cat else []
        ident = f"block:{_norm(entry.name)}"
        item = _find(unit, ident) or Item(id=ident, name=entry.name, kind="quiz")
        if cat:
            blocked.add(cat)
        if not item.locked("weight"):
            item.weight, item.weight_from = entry.weight, entry.source
        item.planned = item.planned if item.locked("planned") else entry.count
        _sync_parts(item, rows_in, news, unit.code)
        _attach(unit, item)

    for cat, rows_in in by_cat.items():
        if cat in blocked:
            continue
        for r in rows_in:
            item = _adopt(unit, r)
            if not item.locked("name"):
                item.name = r.name
            if not item.locked("kind"):
                item.kind = r.kind
            if not item.locked("out_of"):
                item.out_of = r.out_of
            if not item.locked("score"):
                if r.score is not None and item.score != r.score:
                    news.append(_news(unit.code, item.name, r.score, r.out_of))
                item.score, item.score_from = r.score, "moodle"
            if not item.locked("weight"):
                _weigh(item, r, plan)
            _attach(unit, item)

    # Assessments the documents describe but Moodle has not created yet. Two
    # of a unit's four pieces can be missing from the gradebook in September
    # and still be most of the marks; leaving them out makes "what is left"
    # wrong by seventy per cent.
    #
    # Only a scheme that adds to 100% is allowed to invent items. A partial
    # read is usually one assignment brief breaking itself into tasks, and
    # those tasks are not assessments of the unit - promoting them to items
    # put "Task 2: Write docs/requirements.md" on the sheet as if it were one.
    if abs(sum(e.weight for e in plan) - 100.0) > 0.5:
        plan = []
    for entry in plan:
        if getattr(entry, "count", None):
            continue
        if any(_similar(entry.name, i.name) > 0.55 for i in unit.items):
            continue
        ident = f"plan:{_norm(entry.name)}"
        if _find(unit, ident):
            continue
        unit.items.append(Item(
            id=ident, name=entry.name, kind="assignment",
            weight=entry.weight, weight_from=entry.source, source="plan"))

    unit.checked_at = time.time()
    return news


def _adopt(unit: Unit, row) -> Item:
    """The sheet's row for this gradebook line, however it got there.

    A piece of work is usually described in the unit overview weeks before
    Moodle grows a gradebook row for it. When the row finally appears it has
    to land on the item the user has already been editing, not beside it.
    """
    item = _find(unit, row.id)
    if item is not None:
        return item
    for i in unit.items:
        if i.source == "plan" and _similar(row.name, i.name) > 0.55:
            i.source = "moodle"
            i.id = row.id            # from here on Moodle's id is the anchor
            return i
    return Item(id=row.id, name=row.name, kind=row.kind)


def _sync_parts(item: Item, rows: list, news: list[str], code: str) -> None:
    """Point a block's parts at the real gradebook rows, padding to `planned`."""
    for i, r in enumerate(rows):
        while len(item.parts) <= i:
            item.parts.append(Part(name=""))
        part = item.parts[i]
        if part.edited:
            continue
        if r.score is not None and part.score != r.score:
            news.append(_news(code, r.name, r.score, r.out_of))
        part.name, part.score, part.out_of = r.name, r.score, r.out_of
    want = item.planned or len(item.parts)
    while len(item.parts) < want:
        item.parts.append(Part(name=f"{item.name} {len(item.parts) + 1}"))


def _weigh(item: Item, row, plan: list) -> None:
    """Find a weight for one item, and record which source gave it.

    The order is deliberate: a document that states the scheme beats a name
    that happens to carry a number, and both beat the range - because a range
    of 0-15 is only *probably* a 15% weight, and is 0-100 far too often to
    trust on its own.
    """
    entry = _closest_entry(item.name, plan)
    if entry is not None:
        item.weight, item.weight_from = entry.weight, entry.source
        return
    named = re.search(r"weight\D{0,4}(\d{1,3}(?:\.\d+)?)\s*%", item.name, re.I)
    if named:
        item.weight, item.weight_from = float(named.group(1)), "item name"
        return
    if row.out_of and row.out_of != 100:
        item.weight, item.weight_from = row.out_of, "range"
        return
    if item.weight is None:
        item.weight_from = ""


def _news(code: str, name: str, score: float, out_of: float) -> str:
    return f"{code} {name}: {score:g}/{out_of:g}"


def _attach(unit: Unit, item: Item) -> None:
    if not any(i.id == item.id for i in unit.items):
        unit.items.append(item)


def _find(unit: Unit, ident: str) -> Item | None:
    return next((i for i in unit.items if i.id == ident), None)


_WORDS = re.compile(r"[a-z0-9]+")


def _norm(s: str) -> str:
    return " ".join(_WORDS.findall(s.lower()))


def _similar(a: str, b: str) -> float:
    """How likely two names are the same assessment.

    Two rules, both learned the hard way against real units:

    A shared number is never enough on its own. "Quiz 1" and "Individual
    Assignment 1" have nothing in common but the digit, and the first run of
    this matched them - putting the assignment's 20% onto Quiz 1, the next
    plan entry's 30% onto Quiz 2, and so on down the list.

    A differing number is disqualifying. "Individual Assignment 1" and
    "Individual Assignment 3" share every word but one, and swapping them puts
    a 20% mark against a 40% assignment.
    """
    ta, tb = _WORDS.findall(a.lower()), _WORDS.findall(b.lower())
    wa = {t for t in ta if not t.isdigit()}
    wb = {t for t in tb if not t.isdigit()}
    da = {t for t in ta if t.isdigit()}
    db = {t for t in tb if t.isdigit()}
    if da and db and not (da & db):
        return 0.0
    if not wa or not wb:
        return 0.0
    shared = sum(1 for w in wa if any(_same_word(w, x) for x in wb))
    if not shared:
        return 0.0
    return shared / min(len(wa), len(wb)) + (0.25 if da & db else 0.0)


def _same_word(a: str, b: str) -> bool:
    """Loose enough that `Quizzes` matches `quiz`, strict enough to stop there.

    The gradebook category is `Quizzes` and the unit overview calls the same
    thing an `in-class quiz`; without this the block never binds to its own
    quizzes and the sheet grows a phantom set of empty ones alongside them.
    """
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= 4 and long.startswith(short)


def _closest(name: str, options: list[str], floor: float = 0.45) -> str:
    best, score = "", floor
    for o in options:
        s = _similar(name, o)
        if s > score:
            best, score = o, s
    return best


def _closest_entry(name: str, plan: list, floor: float = 0.55):
    best, score = None, floor
    for e in plan:
        if getattr(e, "count", None):
            continue  # blocks are matched by category, not by item name
        s = _similar(name, e.name)
        if s > score:
            best, score = e, s
    return best


def _num(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(v: object) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def default_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local"))
    return base / "moodle-downloader" / Book.FILE
