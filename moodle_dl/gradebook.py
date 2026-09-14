"""Read the marks out of Moodle's user grade report.

The report is the only page that carries real marks, and it carries almost
nothing else: four columns, `Grade item | Grade | Range | Feedback`. Every
Monash unit checked has the weight and contribution columns turned off and no
course total row, so this module's job stops at "what did I score, out of
what" - the weights come from assessplan.py and from the user.

The range is the part that is easy to overlook and impossible to do without.
A bare `21` means nothing: out of 25 it is most of the marks, out of 100 it is
a disaster. Everything downstream divides by it.
"""
from __future__ import annotations

import dataclasses
import re

from bs4 import BeautifulSoup

from .config import Config, Unit
from .downloader import demojibake
from .session import MoodleSession

REPORT = "/grade/report/user/index.php?id={course_id}"

# Moodle prints the range with an en dash, and a hyphen on some themes.
_RANGE = re.compile(r"(-?[\d.]+)\s*[\u2012-\u2015\u2212-]\s*(-?[\d.]+)")
_ROW_ID = re.compile(r"row_(\d+)_")
_CAT_ID = re.compile(r"cat_(\d+)")

# The activity icon's alt text, mapped to what the sheet calls things. Every
# assessed thing is an "assignment" to the user; the kind only changes wording.
_KINDS = {
    "assignment": "assignment",
    "quiz": "quiz",
    "external tool": "other",
    "feedback": "other",
    "workshop": "assignment",
    "lesson": "other",
}
# Whole words only: "Software Demonstration and Testing" is an assignment, and
# calling it a test because the letters are in there reads as a parsing bug.
_BY_NAME = (
    (re.compile(r"\bexams?\b", re.I), "exam"),
    (re.compile(r"\bfinal exam\b", re.I), "exam"),
    (re.compile(r"\btests?\b", re.I), "test"),
    (re.compile(r"\bquizz?e?s?\b", re.I), "quiz"),
)


@dataclasses.dataclass
class RawItem:
    """One graded line exactly as Moodle presents it."""

    id: str
    name: str
    kind: str
    category: str          # "" when the item sits directly under the unit
    score: float | None
    out_of: float
    url: str = ""


def fetch(sess: MoodleSession, cfg: Config, unit: Unit) -> list[RawItem]:
    url = cfg.base_url + REPORT.format(course_id=unit.course_id)
    return parse(sess.get_html(url))


def parse(html: str) -> list[RawItem]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.user-grade")
    if table is None:
        return []

    # Category names, so a grouped item can say which block it belongs to.
    # The top-level one is the unit itself and is not a grouping.
    cats: dict[str, str] = {}
    root: str | None = None
    for th in table.select("th.category"):
        m = re.match(r"cat_(\d+)_", th.get("id") or "")
        if not m:
            continue
        name = _clean(th.get_text(" ", strip=True))
        cats[m.group(1)] = name
        if "level1" in (th.get("class") or []) and root is None:
            root = m.group(1)

    items: list[RawItem] = []
    for tr in table.find_all("tr"):
        cell = tr.select_one(".item.column-itemname")
        if cell is None:
            continue

        link = cell.select_one("a.gradeitemheader")
        name = _clean(link.get_text(" ", strip=True) if link
                      else cell.get_text(" ", strip=True))
        if not name:
            continue

        icon = cell.select_one("img.itemicon")
        alt = (icon.get("alt") or "").strip().lower() if icon else ""
        kind = _KINDS.get(alt, "assignment")
        for pattern, k in _BY_NAME:
            if pattern.search(name):
                kind = k
                break

        # Prefer Moodle's own row id: it survives a rename, which a name-based
        # key would not, and a renamed item must not read as a new one.
        rid = _ROW_ID.search(cell.get("id") or "")
        ident = rid.group(1) if rid else f"name:{name.lower()}"

        own = [c for c in (tr.get("class") or []) if _CAT_ID.fullmatch(c)]
        cat_id = _CAT_ID.fullmatch(own[-1]).group(1) if own else ""
        category = cats.get(cat_id, "") if cat_id and cat_id != root else ""

        score = _grade(tr)
        out_of = _out_of(tr)
        items.append(RawItem(id=ident, name=name, kind=kind, category=category,
                             score=score, out_of=out_of,
                             url=(link.get("href") or "") if link else ""))
    return items


def _clean(text: str) -> str:
    """Names arrive with the icon's label glued on, and sometimes mis-decoded."""
    text = demojibake(text)
    for junk in ("Actions Grade analysis", "Grade analysis", "Actions"):
        text = text.replace(junk, " ")
    return re.sub(r"\s{2,}", " ", text).strip()


def _grade(tr) -> float | None:
    cell = tr.select_one(".column-grade")
    if cell is None:
        return None
    # The cell also holds an actions dropdown; taking its text as a number
    # would silently swallow the real one.
    copy = BeautifulSoup(str(cell), "html.parser")
    for junk in copy.select(".action-menu, .moodle-actionmenu, script"):
        junk.decompose()
    text = copy.get_text(" ", strip=True)
    m = re.search(r"-?[\d.]+", text.replace(",", ""))
    if not m or text.strip() in {"-", ""}:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def _out_of(tr) -> float:
    cell = tr.select_one(".column-range")
    if cell is None:
        return 100.0
    m = _RANGE.search(cell.get_text(" ", strip=True))
    if not m:
        return 100.0
    try:
        top = float(m.group(2))
    except ValueError:
        return 100.0
    return top if top > 0 else 100.0
